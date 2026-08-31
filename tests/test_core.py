from __future__ import annotations

import sqlite3
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.bootstrap import hub_status, initialize_hub
from orchestrator.core.config import load_team_spec
from orchestrator.core.models import (
    AttemptState,
    MessageEnvelope,
    Recipient,
    TaskState,
)
from orchestrator.core.state_machine import (
    ATTEMPT_TRANSITIONS,
    TASK_TRANSITIONS,
    InvalidTransition,
    ensure_attempt_transition,
    ensure_task_transition,
)
from orchestrator.poc.fake_demo import run_fake_demo
from orchestrator.poc.git_demo import run_git_demo
from orchestrator.poc.recovery_demo import run_recovery_demo
from orchestrator.poc.real_demo import REQUIRED_REAL_CHECKS, assess_real_poc
from orchestrator.storage.sqlite_store import SCHEMA, SQLiteStateStore
from orchestrator.storage.sqlite_store import FencedAttemptError
from orchestrator.workspace.git_manager import GitCommandError, GitWorkspaceManager


class StateMachineContractTests(unittest.TestCase):
    def test_all_declared_task_transitions_are_accepted(self) -> None:
        scenarios = 0
        for current, targets in TASK_TRANSITIONS.items():
            for target in targets:
                ensure_task_transition(current, target)
                scenarios += 1
        self.assertEqual(scenarios, 15)

    def test_representative_invalid_task_transitions_are_rejected(self) -> None:
        cases = (
            (TaskState.PENDING, TaskState.COMPLETED),
            (TaskState.READY, TaskState.REVIEW),
            (TaskState.ACTIVE, TaskState.COMPLETED),
            (TaskState.COMPLETED, TaskState.READY),
            (TaskState.CANCELLED, TaskState.READY),
        )
        for current, target in cases:
            with self.subTest(current=current, target=target):
                with self.assertRaises(InvalidTransition):
                    ensure_task_transition(current, target)

    def test_all_declared_attempt_transitions_are_accepted(self) -> None:
        scenarios = 0
        for current, targets in ATTEMPT_TRANSITIONS.items():
            for target in targets:
                ensure_attempt_transition(current, target)
                scenarios += 1
        self.assertEqual(scenarios, 12)

    def test_terminal_attempts_cannot_be_reused(self) -> None:
        for current in (
            AttemptState.ACCEPTED,
            AttemptState.REJECTED,
            AttemptState.STALE,
            AttemptState.FAILED,
            AttemptState.CANCELLED,
        ):
            with self.subTest(current=current):
                with self.assertRaises(InvalidTransition):
                    ensure_attempt_transition(current, AttemptState.RUNNING)

    def test_real_poc_assessment_requires_every_check(self) -> None:
        checks = {name: True for name in REQUIRED_REAL_CHECKS}
        self.assertEqual(assess_real_poc(checks), "ready")
        checks["review_a_passed"] = False
        self.assertEqual(assess_real_poc(checks), "error")
        checks.pop("review_a_passed")
        self.assertEqual(assess_real_poc(checks), "error")


class ConfigTests(unittest.TestCase):
    def test_poc_team_matches_confirmed_account_boundary(self) -> None:
        spec = load_team_spec(Path("config/team.yaml"))
        counts = {pool.pool_id: pool.count for pool in spec.agent_pools}
        self.assertEqual(counts["codex-supervisor"], 1)
        self.assertEqual(counts["codebuddy-workers"], 2)

    def test_initialize_and_status_use_runtime_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_dir = root / "config"
            config_dir.mkdir()
            config = config_dir / "team.yaml"
            config.write_text(Path("config/team.yaml").read_text(encoding="utf-8"))
            initialized = initialize_hub(root)
            self.assertEqual(initialized["status"], "ready")
            self.assertTrue((root / ".agent-hub/state/agent-hub.db").is_file())
            self.assertEqual(hub_status(root)["summary"]["runs"], 0)

    def test_status_can_select_one_run_and_reports_unknown_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / ".agent-hub/state/agent-hub.db"
            with SQLiteStateStore(database) as store:
                store.create_run("run-a", "team-1")
                store.create_task("run-a", "task-a")
                store.create_run("run-b", "team-1")
                store.create_task("run-b", "task-b")
            selected = hub_status(root, run_id="run-a")
            self.assertEqual(selected["status"], "ready")
            self.assertEqual(selected["summary"]["runs"], 1)
            self.assertEqual(selected["summary"]["tasks"], {"PENDING": 1})
            self.assertEqual(hub_status(root, run_id="missing")["status"], "not-found")


class MessageTests(unittest.TestCase):
    def test_envelope_is_structured_and_serializable(self) -> None:
        message = MessageEnvelope(
            message_id="msg-1",
            team_id="team-1",
            run_id="run-1",
            task_id="task-1",
            sender_agent_id="worker-1",
            recipients=(Recipient("agent", "supervisor-1"),),
            kind="artifact.submitted",
            payload={"summary": "done"},
            correlation_id="corr-1",
            idempotency_key="idem-1",
        )
        self.assertEqual(message.to_dict()["recipients"][0]["id"], "supervisor-1")

    def test_envelope_rejects_missing_recipient(self) -> None:
        with self.assertRaises(ValueError):
            MessageEnvelope(
                message_id="msg-1",
                team_id="team-1",
                run_id="run-1",
                task_id="task-1",
                sender_agent_id="worker-1",
                recipients=(),
                kind="artifact.submitted",
                correlation_id="corr-1",
                idempotency_key="idem-1",
            )


class SQLiteStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteStateStore(Path(self.temp.name) / "state.db")
        self.store.create_run("run-1", "team-1")
        self.store.create_task("run-1", "task-1")

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def test_transition_and_event_are_committed_together(self) -> None:
        self.store.transition_task("task-1", TaskState.READY, reason="deps-ready")
        self.assertEqual(self.store.task_state("task-1"), TaskState.READY)
        event = self.store.events(task_id="task-1")[-1]
        self.assertEqual(event["from_state"], TaskState.PENDING.value)
        self.assertEqual(event["to_state"], TaskState.READY.value)

    def test_invalid_transition_changes_neither_state_nor_events(self) -> None:
        before = len(self.store.events(task_id="task-1"))
        with self.assertRaises(InvalidTransition):
            self.store.transition_task(
                "task-1", TaskState.COMPLETED, reason="invalid-shortcut"
            )
        self.assertEqual(self.store.task_state("task-1"), TaskState.PENDING)
        self.assertEqual(len(self.store.events(task_id="task-1")), before)

    def test_attempt_creation_activates_task_atomically(self) -> None:
        self.store.transition_task("task-1", TaskState.READY, reason="deps-ready")
        number = self.store.create_attempt("task-1", "attempt-1", "worker-1")
        self.assertEqual(number, 1)
        self.assertEqual(self.store.task_state("task-1"), TaskState.ACTIVE)
        self.assertEqual(
            self.store.attempt_state("attempt-1"), AttemptState.ASSIGNED
        )

    def test_rework_creates_a_new_attempt_without_reusing_history(self) -> None:
        self.store.transition_task("task-1", TaskState.READY, reason="deps-ready")
        self.store.create_attempt("task-1", "attempt-1", "worker-1")
        self.store.transition_attempt(
            "attempt-1", AttemptState.RUNNING, reason="worker-started"
        )
        generation = self.store.attempt_generation("attempt-1")
        self.store.submit_attempt("attempt-1", generation, reason="artifact-ready")
        self.store.transition_attempt(
            "attempt-1", AttemptState.REJECTED, reason="review-rework"
        )
        self.store.transition_task("task-1", TaskState.READY, reason="rework")
        number = self.store.create_attempt("task-1", "attempt-2", "worker-1")
        self.assertEqual(number, 2)
        self.assertEqual(
            self.store.attempt_state("attempt-1"), AttemptState.REJECTED
        )

    def test_duplicate_message_idempotency_key_is_rejected(self) -> None:
        message = MessageEnvelope(
            message_id="msg-1",
            team_id="team-1",
            run_id="run-1",
            task_id="task-1",
            sender_agent_id="worker-1",
            recipients=(Recipient("agent", "supervisor-1"),),
            kind="artifact.submitted",
            correlation_id="corr-1",
            idempotency_key="same-effect",
        )
        self.store.append_message(message)
        duplicate = MessageEnvelope(
            message_id="msg-2",
            team_id="team-1",
            run_id="run-1",
            task_id="task-1",
            sender_agent_id="worker-1",
            recipients=(Recipient("agent", "supervisor-1"),),
            kind="artifact.submitted",
            correlation_id="corr-1",
            idempotency_key="same-effect",
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.append_message(duplicate)

    def test_summary_can_be_scoped_to_one_run(self) -> None:
        self.store.create_run("run-2", "team-1")
        self.store.create_task("run-2", "task-2")
        scoped = self.store.summary(run_id="run-1")
        self.assertEqual(scoped["runs"], 1)
        self.assertEqual(scoped["tasks"], {"PENDING": 1})
        self.assertEqual(scoped["attempts"], {})
        self.assertEqual(scoped["messages"], 0)
        self.assertGreaterEqual(scoped["events"], 2)
        self.assertEqual(self.store.summary()["runs"], 2)

    def test_reconciler_recovers_only_expired_attempts_and_is_idempotent(self) -> None:
        from orchestrator.reconciler import reconcile_once

        self.store.transition_task("task-1", TaskState.READY, reason="ready")
        lease = self.store.create_attempt_with_lease(
            "task-1", "attempt-1", "worker-1", lease_seconds=60
        )
        self.store.transition_attempt(
            "attempt-1", AttemptState.RUNNING, reason="started"
        )

        before_expiry = reconcile_once(
            self.store, now="2000-01-01T00:00:00+00:00"
        )
        self.assertEqual(before_expiry["recovered"], [])
        self.assertEqual(self.store.attempt_state("attempt-1"), AttemptState.RUNNING)

        recovered = reconcile_once(
            self.store, now="9999-01-01T00:00:00+00:00"
        )
        self.assertEqual(
            recovered["recovered"],
            [
                {
                    "attempt_id": "attempt-1",
                    "generation": lease["generation"],
                    "outcome": "requeued",
                }
            ],
        )
        self.assertEqual(self.store.attempt_state("attempt-1"), AttemptState.STALE)
        self.assertEqual(self.store.task_state("task-1"), TaskState.READY)

        repeated = reconcile_once(
            self.store, now="9999-01-01T00:00:00+00:00"
        )
        self.assertEqual(repeated["recovered"], [])

    def test_reconciler_does_not_recover_a_lease_renewed_after_scan(self) -> None:
        from orchestrator.reconciler import reconcile_once

        self.store.transition_task("task-1", TaskState.READY, reason="ready")
        lease = self.store.create_attempt_with_lease(
            "task-1", "attempt-1", "worker-1"
        )
        self.store.transition_attempt(
            "attempt-1", AttemptState.RUNNING, reason="started"
        )
        with self.store.connection:
            self.store.connection.execute(
                "UPDATE assignment_leases SET expires_at = ? WHERE attempt_id = ?",
                ("2020-01-01T00:00:00+00:00", "attempt-1"),
            )
        candidates = self.store.expired_active_attempts(
            now="2020-01-01T00:00:00+00:00"
        )
        self.assertEqual([item["attempt_id"] for item in candidates], ["attempt-1"])

        self.store.heartbeat_attempt(
            "attempt-1",
            lease["generation"],
            now="2019-12-31T23:59:59+00:00",
            lease_seconds=172800,
        )
        result = reconcile_once(
            self.store, now="2020-01-01T00:00:00+00:00"
        )
        self.assertEqual(result["recovered"], [])
        self.assertEqual(self.store.attempt_state("attempt-1"), AttemptState.RUNNING)
        self.assertEqual(self.store.task_state("task-1"), TaskState.ACTIVE)

    def test_expired_lease_cannot_heartbeat_or_submit(self) -> None:
        self.store.transition_task("task-1", TaskState.READY, reason="ready")
        lease = self.store.create_attempt_with_lease(
            "task-1", "attempt-1", "worker-1"
        )
        self.store.transition_attempt(
            "attempt-1", AttemptState.RUNNING, reason="started"
        )
        with self.store.connection:
            self.store.connection.execute(
                "UPDATE assignment_leases SET expires_at = ? WHERE attempt_id = ?",
                ("2000-01-01T00:00:00+00:00", "attempt-1"),
            )
        with self.assertRaises(FencedAttemptError):
            self.store.heartbeat_attempt(
                "attempt-1",
                lease["generation"],
                now="2000-01-01T00:00:00+00:00",
            )
        with self.assertRaises(FencedAttemptError):
            self.store.submit_attempt(
                "attempt-1", lease["generation"], reason="too-late"
            )
        self.assertEqual(self.store.attempt_state("attempt-1"), AttemptState.RUNNING)

    def test_lost_attempt_requeues_and_fences_old_generation(self) -> None:
        self.store.transition_task("task-1", TaskState.READY, reason="deps-ready")
        lease_1 = self.store.create_attempt_with_lease(
            "task-1", "attempt-1", "worker-1"
        )
        self.store.transition_attempt(
            "attempt-1", AttemptState.RUNNING, reason="started"
        )
        outcome = self.store.recover_lost_attempt(
            "attempt-1", lease_1["generation"], reason="worker_process_exited"
        )
        self.assertEqual(outcome, "requeued")
        self.assertEqual(self.store.task_state("task-1"), TaskState.READY)
        lease_2 = self.store.create_attempt_with_lease(
            "task-1", "attempt-2", "worker-2"
        )
        self.assertGreater(lease_2["generation"], lease_1["generation"])
        with self.assertRaises(FencedAttemptError):
            self.store.submit_attempt(
                "attempt-1", lease_1["generation"], reason="late"
            )
        with self.assertRaises(FencedAttemptError):
            self.store.heartbeat_attempt("attempt-1", lease_1["generation"])

    def test_retry_exhaustion_is_an_explicit_task_failure(self) -> None:
        self.store.create_task("run-1", "single-try", max_attempts=1)
        self.store.transition_task("single-try", TaskState.READY, reason="ready")
        lease = self.store.create_attempt_with_lease(
            "single-try", "single-attempt", "worker-1"
        )
        outcome = self.store.recover_lost_attempt(
            "single-attempt", lease["generation"], reason="worker_process_exited"
        )
        self.assertEqual(outcome, "failed")
        self.assertEqual(self.store.task_state("single-try"), TaskState.FAILED)
        self.assertEqual(
            self.store.task_terminal_reason("single-try"), "worker_process_exited"
        )

    def test_recovery_is_idempotent(self) -> None:
        self.store.transition_task("task-1", TaskState.READY, reason="deps-ready")
        lease = self.store.create_attempt_with_lease(
            "task-1", "attempt-1", "worker-1"
        )
        first = self.store.recover_lost_attempt(
            "attempt-1", lease["generation"], reason="worker_process_exited"
        )
        event_count = len(self.store.events(task_id="task-1"))
        second = self.store.recover_lost_attempt(
            "attempt-1", lease["generation"], reason="worker_process_exited"
        )
        self.assertEqual(first, second)
        self.assertEqual(len(self.store.events(task_id="task-1")), event_count)

    def test_recovery_rolls_back_if_event_append_fails(self) -> None:
        self.store.transition_task("task-1", TaskState.READY, reason="deps-ready")
        lease = self.store.create_attempt_with_lease(
            "task-1", "attempt-1", "worker-1"
        )
        self.store.transition_attempt(
            "attempt-1", AttemptState.RUNNING, reason="started"
        )
        with patch.object(
            self.store, "_append_event", side_effect=RuntimeError("injected")
        ):
            with self.assertRaises(RuntimeError):
                self.store.recover_lost_attempt(
                    "attempt-1",
                    lease["generation"],
                    reason="worker_process_exited",
                )
        self.assertEqual(self.store.task_state("task-1"), TaskState.ACTIVE)
        self.assertEqual(
            self.store.attempt_state("attempt-1"), AttemptState.RUNNING
        )


class SQLiteMigrationTests(unittest.TestCase):
    def test_v1_database_is_migrated_without_losing_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "v1.db"
            connection = sqlite3.connect(database)
            connection.executescript(SCHEMA)
            connection.execute(
                "INSERT INTO runs VALUES ('run-old', 'team-old', '2026-01-01')"
            )
            connection.execute(
                """
                INSERT INTO tasks(
                    task_id, run_id, state, access_mode, write_scope_json,
                    version, created_at, updated_at
                ) VALUES ('task-old', 'run-old', 'PENDING', 'read_only', '[]',
                          0, '2026-01-01', '2026-01-01')
                """
            )
            connection.execute("PRAGMA user_version=1")
            connection.commit()
            connection.close()
            with SQLiteStateStore(database) as migrated:
                self.assertEqual(
                    migrated.task_state("task-old"), TaskState.PENDING
                )
                version = migrated.connection.execute(
                    "PRAGMA user_version"
                ).fetchone()[0]
                self.assertEqual(version, 11)


class FakeWalkingSkeletonTests(unittest.TestCase):
    def test_two_workers_overlap_and_one_reworks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = run_fake_demo(
                Path(directory), database_path=Path(".agent-hub/state/test.db")
            )
        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["checks"]["workers_overlapped"])
        self.assertTrue(result["checks"]["worker_b_first_attempt_rejected"])
        self.assertTrue(result["checks"]["worker_b_rework_accepted"])

    def test_killed_worker_is_requeued_or_explicitly_failed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = run_recovery_demo(
                Path(directory), database_path=Path(".agent-hub/state/test.db")
            )
        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["checks"]["late_submit_was_fenced"])
        self.assertTrue(result["checks"]["retry_exhaustion_failed_task"])

    @unittest.skipUnless(shutil.which("git"), "git is required")
    def test_git_worktrees_integrate_and_conflict_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess_result = subprocess.run(
                ("git", "init", "--initial-branch=main"),
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(subprocess_result.returncode, 0)
            (root / ".gitignore").write_text(".agent-hub/\n", encoding="utf-8")
            result = run_git_demo(root)
        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["checks"]["same_path_conflict_blocked"])
        self.assertTrue(result["checks"]["user_checkout_status_unchanged"])
        self.assertTrue(result["checks"]["user_checkout_contents_unchanged"])
        self.assertTrue(result["checks"]["worker_execution_overlapped"])

    @unittest.skipUnless(shutil.which("git"), "git is required")
    def test_workspace_manager_rejects_unmanaged_and_reserved_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = GitWorkspaceManager(root / "repo", root / "worktrees")
            base = manager.initialize_repository()
            managed = manager.create_worktree("worker", base)
            with self.assertRaises(ValueError):
                manager.commit_file(root, "outside.txt", "bad", "bad")
            with self.assertRaises(ValueError):
                manager.commit_file(managed, ".git", "bad", "bad")
            with self.assertRaises(ValueError):
                manager.commit_file(managed, "../outside.txt", "bad", "bad")

    @unittest.skipUnless(shutil.which("git"), "git is required")
    def test_workspace_manager_rejects_invalid_commit_as_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = GitWorkspaceManager(root / "repo", root / "worktrees")
            manager.initialize_repository()
            with self.assertRaises(GitCommandError):
                manager.integrate("0000000000000000000000000000000000000000")


if __name__ == "__main__":
    unittest.main()
