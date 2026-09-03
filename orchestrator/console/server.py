"""网页控制台（HTTP 服务）。

本地单用户产品控制台：
- Runs：列出/新建 Run，按保存的 team 启动/停止 serve 子进程，只读详情。
- Teams：默认 team + 已保存 team；鼠标组建临时 team（backend/role/count）。
- Connections：探测 codex / codebuddy 登录态并引导登录（不存储凭证）。

协调写操作（取消/暂停/恢复）在操作时临时 acquire controller/authority，
serve 子进程持权期间返回 busy。发起任务不需要协调权。

用法：
    & '.venv\\Scripts\\python.exe' -m orchestrator console --port 8080
    & '.venv\\Scripts\\python.exe' -m orchestrator console --run run-1 --port 8080
"""

from __future__ import annotations

import argparse
import json
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from orchestrator.console.serve_manager import ServeProcessManager
from orchestrator.storage.sqlite_store import (
    FencedAuthorityError,
    FencedControllerError,
    SQLiteStateStore,
)

try:
    from orchestrator.core.models import TaskState
except Exception:  # pragma: no cover
    TaskState = None  # type: ignore[assignment]


def _rows(rows: list[Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


class ConsoleHandler(BaseHTTPRequestHandler):
    server: "ConsoleHTTPServer"

    # -- 基础设施 -----------------------------------------------------------

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json({"error": message}, status=status)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    # -- 路由 ---------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in {"/", "/index.html"}:
            self._serve_index()
            return
        if path == "/api/status":
            self._get_status()
            return
        if path == "/api/connections":
            self._get_connections()
            return
        if path == "/api/teams":
            self._get_teams()
            return
        if path == "/api/runs":
            self._get_runs()
            return
        if path.startswith("/api/runs/"):
            parts = path.strip("/").split("/")
            # GET /api/runs/{run_id}/serve/status
            if len(parts) >= 5 and parts[3] == "serve" and parts[4] == "status":
                self._send_json(self.server.serve_manager.status(parts[2]))
                return
            self._get_run_resource(path)
            return
        self._send_error_json(404, "not found")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        body = self._read_json_body()
        if path == "/api/runs":
            self._create_run(body)
            return
        if path == "/api/teams":
            self._save_team(body)
            return
        if path.startswith("/api/runs/"):
            self._post_run_action(path, body)
            return
        self._send_error_json(404, "not found")

    def _save_team(self, body: dict[str, Any]) -> None:
        from orchestrator.console.settings import save_team_config
        from orchestrator.core.config import load_team_spec

        team = body.get("team")
        if not isinstance(team, dict):
            self._send_error_json(400, "body.team must be a team object")
            return
        try:
            # 校验 team 结构可用 load_team_spec 解析
            as_default = bool(body.get("as_default"))
            if as_default:
                # 覆盖项目默认 team（config/team.yaml）
                target = self.server.project_root / "config" / "team.yaml"
            else:
                target = save_team_config(self.server.project_root, team)
            target.write_text(
                json.dumps(team, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            load_team_spec(target)
        except Exception as exc:  # noqa: BLE001
            self._send_error_json(400, f"{type(exc).__name__}: {exc}")
            return
        self._send_json({"ok": True, "path": str(target)})

    # -- 只读 API -----------------------------------------------------------

    def _get_status(self) -> None:
        self._send_json(
            {
                "project_root": str(self.server.project_root),
                "db": str(self.server.db_path),
                "run_id": self.server.initial_run_id,
                "serve": self.server.serve_manager.status()["runs"],
            }
        )

    def _get_connections(self) -> None:
        from orchestrator.console.settings import probe_connections

        self._send_json({"connections": probe_connections()})

    def _get_teams(self) -> None:
        from orchestrator.console.settings import list_saved_teams

        self._send_json({"teams": list_saved_teams(self.server.project_root)})

    def _get_runs(self) -> None:
        store = self.server.store
        rows = store.connection.execute(
            "SELECT run_id, team_id, control_state, created_at FROM runs "
            "ORDER BY created_at, run_id"
        ).fetchall()
        serve_status = self.server.serve_manager.status()["runs"]
        runs: list[dict[str, Any]] = []
        for row in rows:
            rid = str(row["run_id"])
            summary = store.summary(run_id=rid)
            task_counts = summary.get("tasks")
            total = (
                sum(task_counts.values())
                if isinstance(task_counts, dict)
                else task_counts
            )
            completed = (
                task_counts.get("COMPLETED", 0)
                if isinstance(task_counts, dict)
                else None
            )
            runs.append(
                {
                    "run_id": rid,
                    "team_id": str(row["team_id"] or ""),
                    "control_state": str(row["control_state"] or ""),
                    "created_at": str(row["created_at"]),
                    "tasks_total": total,
                    "tasks_completed": completed,
                    "running": serve_status.get(rid, {}).get("running", False),
                }
            )
        self._send_json({"runs": runs})

    def _get_run_resource(self, path: str) -> None:
        parts = path.strip("/").split("/")
        # /api/runs/{run_id}[/{resource}]  resource: summary|tasks|events|merges|approvals|outbox|agents
        if len(parts) < 3 or parts[0] != "api" or parts[1] != "runs":
            self._send_error_json(404, "not found")
            return
        run_id = parts[2]
        resource = parts[3] if len(parts) > 3 else "summary"
        store = self.server.store
        try:
            if resource == "summary":
                payload = store.summary(run_id=run_id)
                payload["run_id"] = run_id
            elif resource == "tasks":
                payload = _rows(
                    store.connection.execute(
                        "SELECT * FROM tasks WHERE run_id=? "
                        "ORDER BY created_at, task_id",
                        (run_id,),
                    ).fetchall()
                )
            elif resource == "events":
                payload = _rows(
                    store.connection.execute(
                        "SELECT * FROM events WHERE run_id=? "
                        "ORDER BY created_at, event_id",
                        (run_id,),
                    ).fetchall()
                )
            elif resource == "merges":
                payload = _rows(
                    store.connection.execute(
                        "SELECT * FROM merge_queue WHERE run_id=? ORDER BY created_at",
                        (run_id,),
                    ).fetchall()
                )
            elif resource == "approvals":
                payload = _rows(
                    store.connection.execute(
                        "SELECT * FROM approval_requests WHERE run_id=? "
                        "ORDER BY created_at",
                        (run_id,),
                    ).fetchall()
                )
            elif resource == "outbox":
                payload = _rows(
                    store.connection.execute(
                        "SELECT * FROM outbox WHERE run_id=? ORDER BY created_at",
                        (run_id,),
                    ).fetchall()
                )
            elif resource == "agents":
                payload = _rows(
                    store.connection.execute(
                        "SELECT * FROM agent_instances ORDER BY pool_id, agent_id"
                    ).fetchall()
                )
            else:
                self._send_error_json(404, f"unknown resource {resource}")
                return
        except Exception as exc:  # noqa: BLE001
            self._send_error_json(500, type(exc).__name__)
            return
        self._send_json({"run_id": run_id, resource: payload})

    # -- 协调写操作（临时 acquire，serve 持权期间 busy） ---------------------

    def _with_control(
        self, run_id: str, fn: Callable[[Any, Any], None]
    ) -> bool:
        """临时 acquire controller+authority 执行 fn；失败返回 False 并回 409。"""
        store = self.server.store
        controller = store.acquire_run_controller(
            run_id, "console-op", lease_seconds=60
        )
        if controller is None:
            self._send_error_json(
                409, "run controller is held by another owner (serve running?)"
            )
            return False
        try:
            try:
                authority = store.acquire_authority(
                    run_id, "console-op", "supervisor", lease_seconds=60
                )
            except FencedAuthorityError:
                authority = None
            if authority is None:
                self._send_error_json(
                    409, "run authority is held by another owner (serve running?)"
                )
                return False
            try:
                fn(controller, authority)
                return True
            except (FencedControllerError, FencedAuthorityError) as exc:
                self._send_error_json(409, str(exc))
                return False
            except ValueError as exc:
                self._send_error_json(400, str(exc))
                return False
            except KeyError as exc:
                self._send_error_json(404, str(exc))
                return False
        finally:
            store.release_run_controller(controller)

    def _post_run_action(self, path: str, body: dict[str, Any]) -> None:
        parts = path.strip("/").split("/")
        if len(parts) < 3 or parts[0] != "api" or parts[1] != "runs":
            self._send_error_json(404, "not found")
            return
        run_id = parts[2]
        action = parts[3] if len(parts) > 3 else ""
        store = self.server.store
        if action == "tasks" and len(parts) >= 6 and parts[5] == "cancel":
            task_id = parts[4]
            self._with_control(
                run_id,
                lambda c, a: store.request_cancel_task(
                    task_id, c, reason=str(body.get("reason") or "console-cancel")
                ),
            ) and self._send_json({"ok": True, "task_id": task_id})
            return
        if action == "tasks" and len(parts) == 4:
            self._create_task(run_id, body)
            return
        if action == "serve" and len(parts) >= 5:
            self._serve_action(run_id, parts[4], body)
            return
        if action == "pause":
            self._with_control(
                run_id,
                lambda c, a: store.pause_run(
                    run_id, c, reason=str(body.get("reason") or "console-pause")
                ),
            ) and self._send_json({"ok": True})
            return
        if action == "resume":
            self._with_control(
                run_id,
                lambda c, a: store.resume_run(
                    run_id, c, reason=str(body.get("reason") or "console-resume")
                ),
            ) and self._send_json({"ok": True})
            return
        self._send_error_json(404, "not found")

    def _create_task(self, run_id: str, body: dict[str, Any]) -> None:
        if TaskState is None:  # pragma: no cover
            self._send_error_json(500, "TaskState unavailable")
            return
        task_id = str(body.get("task_id") or f"task-{uuid.uuid4().hex[:12]}")
        access_mode = str(body.get("access_mode") or "read_only")
        write_scope = tuple(body.get("write_scope") or ())
        prompt = str(body.get("prompt") or task_id)
        cwd = str(body.get("cwd") or self.server.worktree or ".")
        timeout = float(body.get("timeout_seconds") or 60)
        try:
            self.server.store.create_task(
                run_id,
                task_id,
                access_mode=access_mode,
                write_scope=write_scope,
                required_role_id=str(body.get("required_role_id") or "worker"),
                prompt=prompt,
                cwd=cwd,
                timeout_seconds=timeout,
            )
            self.server.store.transition_task(task_id, TaskState.READY, reason="console")
        except Exception as exc:  # noqa: BLE001
            self._send_error_json(400, f"{type(exc).__name__}: {exc}")
            return
        self._send_json({"ok": True, "task_id": task_id})

    def _serve_action(self, run_id: str, action: str, body: dict[str, Any]) -> None:
        manager = self.server.serve_manager
        if action == "start":
            team_path = Path(str(body.get("team_path") or ""))
            if not team_path.is_absolute():
                team_path = self.server.project_root / team_path
            if not team_path.is_file():
                self._send_error_json(400, f"team file not found: {team_path}")
                return
            result = manager.start(
                run_id, team_path, db_path=self.server.db_path
            )
            self._send_json(result)
            return
        if action == "stop":
            self._send_json(manager.stop(run_id))
            return
        if action == "status":
            self._send_json(manager.status(run_id))
            return
        self._send_error_json(404, "unknown serve action")

    def _create_run(self, body: dict[str, Any]) -> None:
        run_id = str(body.get("run_id") or f"run-{uuid.uuid4().hex[:12]}")
        team_id = str(body.get("team_id") or "default")
        try:
            self.server.store.create_run(run_id, team_id)
        except Exception as exc:  # noqa: BLE001
            self._send_error_json(409, f"{type(exc).__name__}: {exc}")
            return
        self._send_json({"ok": True, "run_id": run_id})

    # -- 静态页 -------------------------------------------------------------

    def _serve_index(self) -> None:
        from orchestrator.console.assets import index_html

        data = index_html().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ConsoleHTTPServer(HTTPServer):
    # 本地单用户工具：单线程串行，避免 sqlite 连接跨线程并发
    def __init__(
        self,
        store: SQLiteStateStore,
        *,
        project_root: Path,
        db_path: Path,
        host: str = "127.0.0.1",
        port: int = 8080,
        initial_run_id: str | None = None,
        worktree: str | None = None,
    ) -> None:
        self.store = store
        self.project_root = project_root
        self.db_path = db_path
        self.initial_run_id = initial_run_id
        self.worktree = worktree
        self.serve_manager = ServeProcessManager(project_root)
        super().__init__((host, port), ConsoleHandler)

    def close(self) -> None:
        self.serve_manager.shutdown_all()
        self.server_close()


def run_console(
    *,
    db: Path,
    project_root: Path,
    port: int = 8080,
    host: str = "127.0.0.1",
    initial_run_id: str | None = None,
    worktree: str | None = None,
) -> None:
    db_path = db if db.is_absolute() else project_root / db
    store = SQLiteStateStore(db_path)
    server = ConsoleHTTPServer(
        store,
        project_root=project_root,
        db_path=db_path,
        host=host,
        port=port,
        initial_run_id=initial_run_id,
        worktree=worktree,
    )
    print(
        f"console listening on http://{host}:{port}  "
        f"(project: {project_root}, db: {db_path})"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path(".agent-hub/state/agent-hub.db"))
    parser.add_argument("--run", dest="run_id", default=None, help="初始打开的 Run")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--worktree", default=None)
    args = parser.parse_args(argv)
    run_console(
        db=args.db,
        project_root=Path.cwd(),
        port=args.port,
        host=args.host,
        initial_run_id=args.run_id,
        worktree=args.worktree,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


