from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.personal_r8_acceptance import (
    FORMAT_VERSION,
    SAMPLE_ID,
    evaluate_evidence,
)
from scripts.personal_r8_real_evidence import build_evidence


def complete_evidence() -> dict:
    return {
        "format_version": FORMAT_VERSION,
        "sample_id": SAMPLE_ID,
        "run_id": "run-r8-test",
        "backend_mode": "real",
        "backends": [{"backend": "codex"}, {"backend": "codebuddy"}],
        "scenarios": {
            "A": {
                "repetitions": 3,
                "worker_calls": ["call-a1", "call-a2"],
                "agent_sessions": ["session-a1", "session-a2"],
                "supervisor_summary_refs": ["artifact://summary-a"],
                "evidence_refs": ["evidence://a"],
            },
            "B": {
                "repetitions": 3,
                "overlap_observed": True,
                "distinct_worktrees": True,
                "distinct_commits": True,
                "user_checkout_unchanged": True,
                "evidence_refs": ["evidence://b"],
            },
            "C": {
                "repetitions": 1,
                "dependency_base_commits": ["abc123"],
                "verified_upstream_refs": ["artifact://upstream"],
                "evidence_refs": ["evidence://c"],
            },
            "D": {
                "repetitions": 3,
                "reviewer_detected_defect": True,
                "rework_attempt": "attempt-r8-rework",
                "post_rework_verification": "evidence://d-final",
                "evidence_refs": ["evidence://d"],
            },
            "E": {
                "repetitions": 1,
                "format_repair_count": 1,
                "out_of_scope_side_effects": 0,
                "fake_complete": False,
                "evidence_refs": ["evidence://e"],
            },
            "F": {
                "repetitions": 1,
                "restart_duplicate_dispatches": 0,
                "duplicate_merges": 0,
                "cancel_state_explicit": "backend_may_still_run=false",
                "evidence_refs": ["evidence://f"],
            },
            "G": {
                "repetitions": 1,
                "guidance_target": "task-r8",
                "delivery_status": "acknowledged",
                "applied_at": "attempt-r8-2",
                "scope_unchanged": True,
                "evidence_refs": ["evidence://g"],
            },
            "H": {
                "repetitions": 1,
                "budget_cap_enforced": True,
                "new_work_after_cap": 0,
                "remaining_reported": "task-r8-remaining",
                "evidence_refs": ["evidence://h"],
            },
            "I": {
                "repetitions": 1,
                "evidence_visible": True,
                "old_evidence_rejected": True,
                "integration_checks_passed": True,
                "evidence_refs": ["evidence://i"],
            },
            "J": {
                "repetitions": 1,
                "same_input_comparison": "run-r8-single-agent",
                "usage_disclosure": "Token usage unavailable; no advantage claimed",
                "manual_interventions_recorded": 2,
                "evidence_refs": ["evidence://j"],
            },
        },
    }


class PersonalR8AcceptanceTests(unittest.TestCase):
    def test_old_stage3_report_is_history_not_r8_completion(self) -> None:
        report = evaluate_evidence(
            None,
            stage3_report={
                "run_id": "run-stage3-old",
                "status": "pass",
                "scenarios_total": 20,
                "passed": 20,
                "results": {
                    "s01": {"backend": "codex"},
                    "s02": {"backend": "codebuddy"},
                },
            },
            source="historical-stage3-only",
        )
        self.assertEqual(report["status"], "CHECKPOINT")
        self.assertFalse(report["historical_stage3"]["usable_as_r8"])
        self.assertTrue(all(item["status"] == "PENDING" for item in report["scenarios"].values()))

    def test_complete_real_evidence_satisfies_all_exits(self) -> None:
        report = evaluate_evidence(complete_evidence())
        self.assertEqual(report["status"], "COMPLETE")
        self.assertEqual(
            {item["status"] for item in report["scenarios"].values()}, {"PASS"}
        )
        self.assertTrue(report["credential_free"])

    def test_safety_failure_cannot_be_hidden_by_other_passes(self) -> None:
        evidence = complete_evidence()
        evidence["scenarios"]["E"]["out_of_scope_side_effects"] = 1
        evidence["scenarios"]["F"]["duplicate_merges"] = 1
        report = evaluate_evidence(evidence)
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["scenarios"]["E"]["status"], "FAIL")
        self.assertEqual(report["scenarios"]["F"]["status"], "FAIL")

    def test_credential_fields_are_removed_from_saved_shape(self) -> None:
        evidence = complete_evidence()
        evidence["api_key"] = "sk-not-for-output"
        evidence["scenarios"]["A"]["nested"] = {"access_token": "secret-value"}
        report = evaluate_evidence(evidence)
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("sk-not-for-output", serialized)
        self.assertNotIn("secret-value", serialized)
        self.assertTrue(report["redacted_fields"])

    def test_missing_repetition_is_pending_not_pass(self) -> None:
        evidence = complete_evidence()
        evidence["scenarios"]["B"]["repetitions"] = 2
        report = evaluate_evidence(evidence)
        self.assertEqual(report["status"], "CHECKPOINT")
        self.assertEqual(report["scenarios"]["B"]["status"], "PENDING")

    def test_real_report_converter_keeps_failures_and_derives_a_b_d(self) -> None:
        checks = {
            key: True
            for key in (
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
        }

        def report(run_id: str, passed: bool) -> dict:
            return {
                "status": "run-passed" if passed else "error",
                "scenario_id": "real-poc-v1",
                "run_id": run_id,
                "checks": checks if passed else {"codex_plan_valid": False},
                "evidence": {
                    "failure": None if passed else {"error_type": "test", "message": "kept"},
                    "workers": {
                        "worker-a-attempt-1": {
                            "status": "completed",
                            "session_id": f"{run_id}-a",
                            "duration_ms": 10,
                            "content_matched": True,
                        },
                        "worker-b-attempt-1": {
                            "status": "completed",
                            "session_id": f"{run_id}-b1",
                            "duration_ms": 11,
                            "content_matched": True,
                        },
                        "worker-b-attempt-2": {
                            "status": "completed",
                            "session_id": f"{run_id}-b2",
                            "duration_ms": 12,
                            "content_matched": True,
                        },
                    },
                    "reviews": {
                        "worker-b-attempt-2": {"decision": {"decision": "PASS"}}
                    },
                    "git": {
                        "worktrees": {
                            "a": f"{run_id}/a",
                            "b1": f"{run_id}/b1",
                            "b2": f"{run_id}/b2",
                        },
                        "worker_a_result_commit": f"{run_id}-ca",
                        "worker_b_rejected_commit": f"{run_id}-cb1",
                        "worker_b_accepted_commit": f"{run_id}-cb2",
                    },
                },
            }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = []
            for run_id, passed in (("run-a", True), ("run-b", True), ("run-failed", False)):
                path = root / f"{run_id}.json"
                path.write_text(json.dumps(report(run_id, passed)), encoding="utf-8")
                paths.append(path)
            evidence = build_evidence(paths, root=root)
        self.assertEqual(evidence["successful_reports"], 2)
        self.assertEqual(len(evidence["failure_records"]), 1)
        self.assertEqual(evidence["scenarios"]["A"]["repetitions"], 2)
        self.assertTrue(evidence["scenarios"]["B"]["distinct_worktrees"])
        self.assertEqual(evidence["scenarios"]["D"]["post_rework_verification"][0]["review"]["decision"], "PASS")


if __name__ == "__main__":
    unittest.main()
