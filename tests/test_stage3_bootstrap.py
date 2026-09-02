from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator.bootstrapper import (
    bootstrap,
    check_prerequisites,
    initialize_workspace,
    python_version_ok,
)


class VersionCheckTests(unittest.TestCase):
    def test_python_version_ok(self) -> None:
        self.assertTrue(python_version_ok("3.12.5"))
        self.assertTrue(python_version_ok("3.10.0"))
        self.assertFalse(python_version_ok("3.9.7"))
        self.assertFalse(python_version_ok("3"))
        self.assertFalse(python_version_ok("not-a-version"))


class PrerequisiteTests(unittest.TestCase):
    def test_prerequisites_list_has_required_tools(self) -> None:
        items = check_prerequisites()
        self.assertEqual(len(items), 4)
        by_name = {item["name"]: item for item in items}
        self.assertTrue(by_name["python"]["required"])
        self.assertTrue(by_name["git"]["required"])
        self.assertFalse(by_name["codex"]["required"])
        self.assertFalse(by_name["codebuddy"]["required"])
        for item in items:
            self.assertIn("ok", item)
            self.assertIn("hint", item)


class WorkspaceInitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_initialize_workspace_creates_hub_dirs(self) -> None:
        cwd = Path(self.temp.name)
        created = initialize_workspace(cwd)
        self.assertEqual(len(created), 5)
        for name in ("state", "reports", "backups", "certs", "logs"):
            self.assertTrue((cwd / ".agent-hub" / name).is_dir())
        # 幂等：再次调用不重复创建
        again = initialize_workspace(cwd)
        self.assertEqual(again, [])


class BootstrapFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_bootstrap_dry_run_reports_required_ok_without_installing(self) -> None:
        result = bootstrap(Path(self.temp.name), dry_run=True)
        self.assertIn("prerequisites", result)
        self.assertNotIn("venv", result)
        self.assertNotIn("status", result)

    def test_create_venv_and_workspace(self) -> None:
        from orchestrator.bootstrapper import create_venv

        project = Path(self.temp.name)
        created = create_venv(project / ".venv")
        self.assertTrue(created)
        self.assertFalse(create_venv(project / ".venv"))  # 幂等
        initialize_workspace(project)
        self.assertTrue((project / ".agent-hub" / "state").is_dir())
        py = project / ".venv" / ("Scripts" if __import__("sys").platform == "win32" else "bin")
        py = py / ("python.exe" if __import__("sys").platform == "win32" else "python")
        self.assertTrue(py.is_file())


if __name__ == "__main__":
    unittest.main()
