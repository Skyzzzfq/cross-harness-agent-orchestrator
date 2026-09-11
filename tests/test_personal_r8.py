from __future__ import annotations

import json
import unittest

from scripts.personal_r8_acceptance import (
    FORMAT_VERSION,
    SAMPLE_ID,
    evaluate_evidence,
)


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


if __name__ == "__main__":
    unittest.main()
