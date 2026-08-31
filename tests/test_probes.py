from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.adapters.codebuddy_config import (
    CODEBUDDY_REGION,
    codebuddy_china_environment,
    preferred_codebuddy_cli,
)
from orchestrator.adapters.contracts import ProbeStatus
from orchestrator.adapters.codex_spike import assess_codex_lifecycle
from orchestrator.adapters.codebuddy_spike import assess_session_isolation
from orchestrator.adapters.codebuddy_safety_spike import (
    _scoped_cli_write,
    path_is_within,
)
from orchestrator.adapters.probes import probe_codebuddy, probe_codex
from orchestrator.adapters.stage1_real import extract_cli_result
from orchestrator.cli import main


class ProbeTests(unittest.TestCase):
    def test_codebuddy_terminal_result_accepts_object_or_single_result_array(self) -> None:
        result = {"type": "result", "subtype": "success", "session_id": "s-1"}
        self.assertEqual(extract_cli_result(json.dumps(result)), result)
        self.assertEqual(extract_cli_result(json.dumps([{"type": "note"}, result])), result)
        self.assertIsNone(extract_cli_result(json.dumps([result, result])))
        self.assertIsNone(extract_cli_result("not-json"))

    def test_codebuddy_defaults_to_china_service(self) -> None:
        self.assertEqual(CODEBUDDY_REGION, "internal")
        self.assertEqual(
            codebuddy_china_environment(),
            {"CODEBUDDY_INTERNET_ENVIRONMENT": "internal"},
        )

    def test_codebuddy_cli_falls_back_when_private_runtime_is_missing(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(preferred_codebuddy_cli(Path("Z:/missing/project")))

    def test_codebuddy_session_assessment_requires_all_isolation_checks(self) -> None:
        workers = [
            {
                "marker": "CODEBUDDY_AGENT_A_OK",
                "cwd": "worker-a",
                "session_id": "session-a",
                "started_at": 1.0,
                "ended_at": 3.0,
                "matched": True,
            },
            {
                "marker": "CODEBUDDY_AGENT_B_OK",
                "cwd": "worker-b",
                "session_id": "session-b",
                "started_at": 1.5,
                "ended_at": 2.5,
                "matched": True,
            },
        ]
        result = assess_session_isolation(workers, {"matched": True})
        self.assertEqual(result["status"], "ready")
        self.assertTrue(all(result["checks"].values()))

        workers[1]["session_id"] = "session-a"
        result = assess_session_isolation(workers, {"matched": True})
        self.assertEqual(result["status"], "error")
        self.assertFalse(result["checks"]["session_ids_are_distinct"])

    def test_codebuddy_safety_path_boundary(self) -> None:
        root = Path("D:/workspace/connect/.agent-hub/spike/allowed")
        self.assertTrue(path_is_within(root / "result.txt", root))
        self.assertFalse(path_is_within(root.parent / "outside.txt", root))

        blocked = _scoped_cli_write(
            root, root.parent / "outside.txt", "MUST_NOT_WRITE"
        )
        self.assertFalse(blocked["invoked_backend"])
        self.assertEqual(
            blocked["policy_decision"], "blocked:outside-allowed-root"
        )

    def test_codex_lifecycle_assessment_requires_every_check(self) -> None:
        checks = {
            "success": True,
            "failure": True,
            "timeout": True,
            "cancel": True,
            "resume": True,
        }
        self.assertEqual(assess_codex_lifecycle(checks), "ready")
        checks["cancel"] = False
        self.assertEqual(assess_codex_lifecycle(checks), "error")

    @patch("orchestrator.adapters.probes._codex_saved_login", return_value=False)
    @patch("orchestrator.adapters.probes.codex_executable", return_value=None)
    @patch("orchestrator.adapters.probes._module_available", return_value=False)
    @patch("orchestrator.adapters.probes._distribution_version", return_value=None)
    def test_codex_is_unavailable_without_sdk_or_cli(self, *_: object) -> None:
        self.assertEqual(probe_codex().status, ProbeStatus.UNAVAILABLE)

    @patch("orchestrator.adapters.probes._codex_saved_login", return_value=True)
    @patch("orchestrator.adapters.probes.codex_executable", return_value="codex")
    @patch("orchestrator.adapters.probes._module_available", return_value=True)
    @patch("orchestrator.adapters.probes._distribution_version", return_value="1.2.3")
    def test_codex_sdk_with_saved_login_is_ready(self, *_: object) -> None:
        result = probe_codex()
        self.assertEqual(result.status, ProbeStatus.READY)
        self.assertEqual(result.auth, "saved-login-present")
        self.assertNotIn("token", json.dumps(result.to_dict()).lower())

    @patch("orchestrator.adapters.probes._module_available", return_value=True)
    @patch("orchestrator.adapters.probes._distribution_version", return_value="2.0.0")
    @patch("orchestrator.adapters.probes._first_executable", return_value=None)
    def test_codebuddy_sdk_is_discoverable(self, *_: object) -> None:
        result = probe_codebuddy()
        self.assertEqual(result.status, ProbeStatus.READY)
        self.assertEqual(result.entrypoint, "python-sdk")

    def test_cli_emits_json(self) -> None:
        with patch("builtins.print") as output:
            self.assertEqual(main(["probe"]), 0)
        json.loads(output.call_args.args[0])

    @patch(
        "orchestrator.cli.initialize_hub",
        return_value={"status": "ready", "team_id": "test-team"},
    )
    def test_cli_initializes_state(self, initialize: object) -> None:
        with patch("builtins.print") as output:
            self.assertEqual(main(["init"]), 0)
        self.assertEqual(json.loads(output.call_args.args[0])["status"], "ready")

    @patch(
        "orchestrator.cli.hub_status",
        return_value={"status": "uninitialized"},
    )
    def test_cli_status_reports_uninitialized(self, status: object) -> None:
        with patch("builtins.print") as output:
            self.assertEqual(main(["status"]), 1)
        self.assertEqual(
            json.loads(output.call_args.args[0])["status"], "uninitialized"
        )

    @patch(
        "orchestrator.cli.run_fake_demo",
        return_value={"status": "ready", "mode": "fake"},
    )
    def test_cli_runs_fake_demo(self, demo: object) -> None:
        with patch("builtins.print") as output:
            self.assertEqual(main(["demo", "--fake"]), 0)
        self.assertEqual(json.loads(output.call_args.args[0])["mode"], "fake")

    @patch(
        "orchestrator.cli.run_git_demo",
        return_value={"status": "ready", "mode": "git-fake"},
    )
    def test_cli_runs_git_fake_demo(self, demo: object) -> None:
        with patch("builtins.print") as output:
            self.assertEqual(main(["demo", "--git-fake"]), 0)
        self.assertEqual(json.loads(output.call_args.args[0])["mode"], "git-fake")

    @patch(
        "orchestrator.cli.run_recovery_demo",
        return_value={"status": "ready", "mode": "recovery-fake"},
    )
    def test_cli_runs_recovery_fake_demo(self, demo: object) -> None:
        with patch("builtins.print") as output:
            self.assertEqual(main(["demo", "--recovery-fake"]), 0)
        self.assertEqual(
            json.loads(output.call_args.args[0])["mode"], "recovery-fake"
        )

    @patch(
        "orchestrator.cli.run_real_demo",
        return_value={"status": "ready", "mode": "real-poc"},
    )
    def test_cli_runs_real_demo(self, demo: object) -> None:
        with patch("builtins.print") as output:
            self.assertEqual(main(["demo", "--real"]), 0)
        self.assertEqual(json.loads(output.call_args.args[0])["mode"], "real-poc")

    @patch(
        "orchestrator.cli.run_reconciler_once",
        return_value={"status": "ready", "mode": "reconcile-once"},
    )
    def test_cli_runs_one_reconciliation_pass(self, reconcile: object) -> None:
        with patch("builtins.print") as output:
            self.assertEqual(main(["reconcile"]), 0)
        self.assertEqual(
            json.loads(output.call_args.args[0])["mode"], "reconcile-once"
        )

    @patch(
        "orchestrator.cli.run_live",
        return_value={"backend": "codex", "status": "ready"},
    )
    def test_cli_emits_live_probe_json(self, run_live: object) -> None:
        with patch("builtins.print") as output:
            self.assertEqual(main(["probe", "--live", "codex"]), 0)
        self.assertEqual(json.loads(output.call_args.args[0])["status"], "ready")

    @patch("orchestrator.cli.login", return_value=0)
    def test_cli_routes_authentication(self, login: object) -> None:
        self.assertEqual(main(["auth", "codex"]), 0)

    @patch(
        "orchestrator.cli.run_codebuddy_session_spike",
        return_value={"backend": "codebuddy", "status": "ready"},
    )
    def test_cli_routes_codebuddy_session_spike(self, spike: object) -> None:
        with patch("builtins.print"):
            self.assertEqual(main(["spike", "codebuddy-sessions"]), 0)


if __name__ == "__main__":
    unittest.main()
