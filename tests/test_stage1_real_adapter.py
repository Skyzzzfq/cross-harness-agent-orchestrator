from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator.adapters import stage1_real


class Stage1RealCodeBuddyCommandTests(unittest.TestCase):
    def test_codebuddy_write_loads_user_and_project_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cli = root / "codebuddy.cmd"
            cli.write_text("placeholder", encoding="utf-8")
            completed = subprocess.CompletedProcess(
                [str(cli)],
                0,
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "structured_output": {"content": "OK"},
                        "session_id": "session-r8-test",
                        "duration_ms": 12,
                    }
                ),
                "",
            )
            with mock.patch.object(
                stage1_real, "preferred_codebuddy_cli", return_value=str(cli)
            ), mock.patch.object(
                stage1_real,
                "_run_managed_process",
                return_value=(completed, True),
            ) as run_process:
                result = stage1_real.run_codebuddy_write(
                    root, "demo/result.txt", "OK", session_id="session-r8-test"
                )
            self.assertEqual(result["status"], "completed")
            command = run_process.call_args.args[0]
            self.assertEqual(
                command[command.index("--setting-sources") + 1], "user,project"
            )
            self.assertEqual(
                (root / "demo" / "result.txt").read_text(encoding="utf-8"),
                "OK\n",
            )


if __name__ == "__main__":
    unittest.main()
