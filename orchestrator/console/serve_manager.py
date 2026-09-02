"""网页控制台：serve 子进程管理（spawn / stop / status）。

子进程命令：``python -m orchestrator serve-team --run <run_id> --team <team>``。
日志写入 ``.agent-hub/logs/serve-<run_id>.log``。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any


def _venv_python(project_root: Path) -> Path:
    """优先使用项目 venv 的 python，否则回退当前解释器。"""
    candidate = project_root / ".venv" / (
        "Scripts" if sys.platform == "win32" else "bin"
    ) / ("python.exe" if sys.platform == "win32" else "python")
    return candidate if candidate.is_file() else Path(sys.executable)


class ServeProcessManager:
    """控制台进程内管理各 Run 的 serve 子进程（本地单用户工具）。"""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self._procs: dict[str, subprocess.Popen[str]] = {}

    def _log_path(self, run_id: str) -> Path:
        path = self.project_root / ".agent-hub" / "logs"
        path.mkdir(parents=True, exist_ok=True)
        return path / f"serve-{run_id}.log"

    def start(
        self,
        run_id: str,
        team_path: Path,
        *,
        db_path: Path | None = None,
        interval: float = 1.0,
        lease: int = 300,
    ) -> dict[str, Any]:
        if run_id in self._procs:
            proc = self._procs[run_id]
            if proc.poll() is None:
                return {"ok": True, "run_id": run_id, "status": "already-running"}
        python = _venv_python(self.project_root)
        command = [
            str(python),
            "-m",
            "orchestrator",
            "serve-team",
            "--run",
            run_id,
            "--team",
            str(team_path),
            "--interval",
            str(interval),
            "--lease",
            str(lease),
        ]
        if db_path is not None:
            command.extend(["--db", str(db_path)])
        log = self._log_path(run_id)
        with open(log, "a", encoding="utf-8") as handle:
            proc = subprocess.Popen(
                command,
                cwd=str(self.project_root),
                stdout=handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
        self._procs[run_id] = proc
        return {"ok": True, "run_id": run_id, "status": "started", "pid": proc.pid}

    def stop(self, run_id: str) -> dict[str, Any]:
        proc = self._procs.get(run_id)
        if proc is None or proc.poll() is not None:
            return {"ok": True, "run_id": run_id, "status": "not-running"}
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        self._procs.pop(run_id, None)
        return {"ok": True, "run_id": run_id, "status": "stopped"}

    def status(self, run_id: str | None = None) -> dict[str, Any]:
        if run_id is not None:
            proc = self._procs.get(run_id)
            return {
                "run_id": run_id,
                "running": proc is not None and proc.poll() is None,
                "pid": proc.pid if proc is not None and proc.poll() is None else None,
            }
        result: dict[str, Any] = {}
        for rid, proc in list(self._procs.items()):
            result[rid] = {
                "running": proc.poll() is None,
                "pid": proc.pid if proc.poll() is None else None,
            }
        return {"runs": result}

    def shutdown_all(self) -> None:
        for run_id in list(self._procs):
            self.stop(run_id)
