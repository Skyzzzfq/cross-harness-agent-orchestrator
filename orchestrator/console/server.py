"""网页控制台（HTTP 服务）。

本地单用户产品控制台：
- Runs：列出/新建 Run，按保存的 team 启动/停止 serve 子进程，只读详情。
- Teams：默认 team + 已保存 team；鼠标组建临时 team（backend/role/count）。
- Connections：探测 codex / codebuddy 登录态并引导登录（不存储凭证）。

协调写操作（取消/暂停/恢复）在操作时临时 acquire controller/authority；
人工审核/删除可在 serve 持权期间复用当前 fencing token，其余操作仍返回 busy。
发起任务不需要协调权。

用法：
    & '.venv\\Scripts\\python.exe' -m orchestrator console --port 8080
    & '.venv\\Scripts\\python.exe' -m orchestrator console --run run-1 --port 8080
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import socket
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from orchestrator.console.serve_manager import ServeProcessManager
from orchestrator.core.models import AuthorityToken, ControllerToken, utc_now
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


def _human_task_prompt(text: str) -> str:
    """Hide the internal supervisor protocol from the user-facing chat."""
    value = str(text or "").strip()
    if "USER GOAL:" in value:
        value = value.split("USER GOAL:", 1)[1]
        for marker in ("\n\nROLE/POOL DIRECTORY:", "\n\nBUDGET:", "\n\nLEGAL PLAN SHAPE:"):
            if marker in value:
                value = value.split(marker, 1)[0]
        value = value.strip()
    return value or str(text or "")


def _human_result_text(
    result: dict[str, Any],
    failure: dict[str, Any],
    *,
    role_id: str = "",
) -> str:
    """Render adapter envelopes as readable text instead of protocol JSON."""
    text = result.get("text")
    structured = result.get("structured")
    candidate: Any = structured
    if isinstance(text, str) and text.strip():
        stripped = text.strip()
        try:
            parsed = json.loads(stripped)
        except (TypeError, ValueError):
            return stripped
        candidate = parsed
    if not isinstance(candidate, dict):
        if text is not None:
            return str(text)
        if failure:
            return str(failure.get("message") or failure.get("error") or "Agent 执行失败")
        return "（Agent 尚未返回内容）"

    summary = candidate.get("summary") or candidate.get("supervisor_response")
    tasks = candidate.get("tasks")
    if isinstance(tasks, list):
        lines: list[str] = []
        if summary:
            lines.append(f"主管：{summary}")
        lines.append(f"主管已完成规划，共 {len(tasks)} 个 Worker 任务：")
        for index, item in enumerate(tasks, 1):
            if not isinstance(item, dict):
                lines.append(f"{index}. {item}")
                continue
            task_id = str(item.get("task_id") or f"任务 {index}")
            instruction = str(item.get("instruction") or item.get("prompt") or "")
            role = str(item.get("role_id") or "worker")
            suffix = f"（{role}）" if role else ""
            lines.append(f"{index}. {task_id}{suffix}：{instruction}".rstrip("："))
        return "\n".join(lines)
    if summary:
        return str(summary)

    # Worker adapters may return a small structured object.  Keep it readable
    # without exposing JSON syntax, while retaining all useful fields.
    lines = []
    for key, value in candidate.items():
        if key in {"plan_version", "revision", "worker_concurrency"}:
            continue
        if isinstance(value, (dict, list)):
            value = ", ".join(str(item) for item in value) if isinstance(value, list) else str(value)
        lines.append(f"{key}: {value}")
    if lines:
        return "\n".join(lines)
    if failure:
        return str(failure.get("message") or failure.get("error") or "Agent 执行失败")
    return "（Agent 返回了空内容）"


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
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/connections/login-status":
            from orchestrator.console.login_flow import status
            self._send_json(status(self.server.project_root))
            return
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
        if path == "/api/projects":
            self._get_projects()
            return
        if path == "/api/recovery":
            self._get_recovery(parse_qs(parsed.query))
            return
        if path == "/api/history/preview":
            self._get_history_preview(parse_qs(parsed.query))
            return
        if path == "/api/model-catalog":
            self._get_model_catalog(parse_qs(parsed.query))
            return
        if path == "/api/model-providers":
            self._get_model_providers()
            return
        if path == "/api/runs":
            self._get_runs()
            return
        if path.startswith("/api/runs/"):
            parts = path.strip("/").split("/")
            # GET /api/runs/{run_id}/serve/status
            if len(parts) >= 5 and parts[3] == "serve" and parts[4] == "status":
                self._send_json(self._serve_status(parts[2]))
                return
            self._get_run_resource(self.path)
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
        if path == "/api/projects":
            self._save_project(body)
            return
        if path == "/api/model-providers":
            self._save_model_provider(body)
            return
        if path == "/api/model-catalog/probe":
            self._probe_model_catalog(body)
            return
        if path == "/api/connections/login":
            self._login_connection(body)
            return
        if path.startswith("/api/runs/"):
            self._post_run_action(path, body)
            return
        self._send_error_json(404, "not found")

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        run_prefix = "/api/runs/"
        if path.startswith(run_prefix):
            run_id = path[len(run_prefix):].strip("/")
            if "/" in run_id or not run_id:
                self._send_error_json(400, "run_id must not be empty")
                return
            self._delete_run(run_id)
            return
        prefix = "/api/model-providers/"
        if path.startswith(prefix):
            provider_id = path[len(prefix):].strip()
            from orchestrator.console.settings import delete_model_provider

            if not provider_id:
                self._send_error_json(400, "provider_id must not be empty")
                return
            if not delete_model_provider(self.server.project_root, provider_id):
                self._send_error_json(404, "model provider not found")
                return
            self._send_json({"ok": True, "provider_id": provider_id})
            return
        project_prefix = "/api/projects/"
        if path.startswith(project_prefix):
            project_id = path[len(project_prefix):].strip()
            from orchestrator.console.settings import delete_project

            if not project_id:
                self._send_error_json(400, "project_id must not be empty")
                return
            if not delete_project(self.server.project_root, project_id):
                self._send_error_json(404, "project not found")
                return
            self._send_json({"ok": True, "project_id": project_id})
            return
        self._send_error_json(404, "not found")

    def _delete_run(self, run_id: str) -> None:
        """Delete only this Run's managed local state after explicit UI confirmation."""
        store = self.server.store
        run = store.connection.execute(
            "SELECT run_id FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if run is None:
            self._send_error_json(404, f"run not found: {run_id}")
            return
        if self._serve_status(run_id).get("running"):
            self._send_error_json(409, "请先停止该 Run 的 serve 后再删除")
            return
        active = store.connection.execute(
            "SELECT task_id, state FROM tasks WHERE run_id=? "
            "AND state IN ('ACTIVE','INTEGRATION','CANCEL_REQUESTED') LIMIT 1",
            (run_id,),
        ).fetchone()
        if active is not None:
            self._send_error_json(
                409,
                f"Run 仍有活动任务 {active['task_id']}（{active['state']}），请先取消或等待结束",
            )
            return

        project_root = self.server.project_root.resolve()
        hub_root = (project_root / ".agent-hub").resolve()
        worktrees_root = (hub_root / "worktrees").resolve()
        runs_root = (hub_root / "runs").resolve()
        candidates = [worktrees_root / run_id, runs_root / run_id]
        manager = getattr(store, "workspace_manager", None)
        if manager is not None and hasattr(manager, "run_root"):
            try:
                candidates.append(Path(manager.run_root(run_id)))
            except (OSError, ValueError):
                pass
        unique: list[Path] = []
        for candidate in candidates:
            resolved = candidate.resolve(strict=False)
            if resolved in unique:
                continue
            # Every removable target is one direct child of a fixed managed
            # root.  This prevents a malformed Run id from reaching the repo.
            if resolved.parent not in {worktrees_root, runs_root}:
                self._send_error_json(400, "run workspace is outside the managed roots")
                return
            unique.append(resolved)

        removed: list[str] = []
        try:
            for target in unique:
                if not target.exists() and not target.is_symlink():
                    continue
                if target.parent == worktrees_root:
                    subprocess.run(
                        ["git", "-C", str(project_root), "worktree", "remove", "--force", str(target)],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                if target.is_symlink():
                    target.unlink()
                elif target.exists():
                    shutil.rmtree(target)
                removed.append(str(target))
        except OSError as exc:
            self._send_error_json(500, f"删除 Run 文件失败：{type(exc).__name__}")
            return

        try:
            deleted = store.mark_run_deleted(run_id, reason="console-delete")
        except ValueError as exc:
            self._send_error_json(409, str(exc))
            return
        except KeyError:
            self._send_error_json(404, f"run not found: {run_id}")
            return
        self.server._run_worktrees.pop(run_id, None)
        self._send_json({"ok": True, "deleted": True, **deleted, "removed_paths": removed})

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

    def _login_connection(self, body: dict[str, Any]) -> None:
        """打开后端登录引导窗口（codex/codebuddy）。"""
        from orchestrator.console.settings import launch_login

        backend = str(body.get("backend") or "")
        if backend not in {"codex", "codebuddy"}:
            self._send_error_json(400, "backend must be codex or codebuddy")
            return
        try:
            if backend == "codebuddy":
                from orchestrator.console.login_flow import start
                result = start(self.server.project_root)
            else:
                result = launch_login(self.server.project_root, backend)
        except Exception as exc:  # noqa: BLE001
            self._send_error_json(500, f"{type(exc).__name__}: {exc}")
            return
        if not result.get("ok"):
            self._send_json({"ok": False, **result})
            return
        self._send_json({"ok": True, **result})

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

        self._send_json(
            {"connections": probe_connections(self.server.project_root)}
        )

    def _get_teams(self) -> None:
        from orchestrator.console.settings import list_saved_teams

        self._send_json({"teams": list_saved_teams(self.server.project_root)})

    def _get_projects(self) -> None:
        from orchestrator.console.settings import list_projects

        self._send_json({"projects": list_projects(self.server.project_root)})

    def _get_recovery(self, query: dict[str, list[str]]) -> None:
        from orchestrator.recovery import recovery_snapshot

        run_id = str((query.get("run_id") or [""])[0] or "").strip() or None
        self._send_json(recovery_snapshot(self.server.store, run_id=run_id))

    def _get_history_preview(self, query: dict[str, list[str]]) -> None:
        from orchestrator.history import history_cleanup_preview

        try:
            days = int((query.get("older_than_days") or ["30"])[0] or 30)
        except ValueError:
            self._send_error_json(400, "older_than_days must be an integer")
            return
        run_id = str((query.get("run_id") or [""])[0] or "").strip() or None
        try:
            payload = history_cleanup_preview(
                self.server.store, older_than_days=days, run_id=run_id
            )
        except ValueError as exc:
            self._send_error_json(400, str(exc))
            return
        self._send_json(payload)

    def _save_project(self, body: dict[str, Any]) -> None:
        from orchestrator.console.settings import save_project

        project = body.get("project") if isinstance(body.get("project"), dict) else body
        try:
            saved = save_project(self.server.project_root, project)
        except ValueError as exc:
            self._send_error_json(400, str(exc))
            return
        self._send_json({"ok": True, "project": saved})

    def _get_model_catalog(self, query: dict[str, list[str]]) -> None:
        from orchestrator.console.model_catalog import get_model_catalog

        backend = str((query.get("backend") or [""])[0]).strip().lower()
        if not backend:
            self._send_error_json(400, "backend query parameter is required")
            return
        self._send_json(
            {"backend": backend, **get_model_catalog(self.server.project_root, backend)}
        )

    def _get_model_providers(self) -> None:
        from orchestrator.console.settings import load_model_providers

        self._send_json(
            {"providers": load_model_providers(self.server.project_root)}
        )

    def _save_model_provider(self, body: dict[str, Any]) -> None:
        from orchestrator.console.settings import save_model_provider

        try:
            provider = save_model_provider(self.server.project_root, body)
        except ValueError as exc:
            self._send_error_json(400, str(exc))
            return
        self._send_json({"ok": True, "provider": provider})

    def _probe_model_catalog(self, body: dict[str, Any]) -> None:
        from orchestrator.console.model_catalog import probe_codebuddy_models

        backend = str(body.get("backend") or "").strip().lower()
        if backend in {"workbuddy", "codebuddy"}:
            backend = "codebuddy"
        if backend != "codebuddy":
            self._send_error_json(
                400, "only the codebuddy model catalog supports connectivity probing"
            )
            return
        provider_id = str(body.get("provider_id") or "").strip() or None
        try:
            self._send_json(
                {
                    "backend": backend,
                    **probe_codebuddy_models(
                        self.server.project_root, provider_id=provider_id
                    ),
                }
            )
        except Exception as exc:  # noqa: BLE001 - do not expose probe details
            self._send_error_json(
                500, f"{type(exc).__name__}: model probe failed"
            )

    def _get_runs(self) -> None:
        store = self.server.store
        rows = store.connection.execute(
            "SELECT r.run_id, r.team_id, r.control_state, r.created_at FROM runs r "
            "LEFT JOIN deleted_runs d ON d.run_id=r.run_id "
            "WHERE d.run_id IS NULL ORDER BY r.created_at, r.run_id"
        ).fetchall()
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
                    "running": self._serve_status(rid).get("running", False),
                }
            )
        self._send_json({"runs": runs})

    def _serve_status(self, run_id: str | None = None) -> dict[str, Any]:
        """Combine local child-process state with the persisted serve lease."""
        status = self.server.serve_manager.status(run_id)
        if run_id is None:
            return status
        active = self.server.store.active_run_controller(run_id)
        if (
            run_id not in self.server._serve_stopped
            and active is not None
            and str(active["owner_id"]).startswith("serve-")
        ):
            status = dict(status)
            status["run_id"] = run_id
            status["running"] = True
            status["controller_owner"] = str(active["owner_id"])
            status["controller_expires_at"] = str(active["expires_at"])
        return status

    def _get_run_resource(self, path: str) -> None:
        parsed = urlparse(path)
        parts = parsed.path.strip("/").split("/")
        query = parse_qs(parsed.query)
        # /api/runs/{run_id}[/{resource}]  resource: summary|tasks|events|merges|approvals|plans|artifacts|handoffs|results|evidence|workspace|outbox|agents
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
                        "SELECT t.*, d.required_role_id, d.required_backend, "
                        "d.required_model, d.required_provider_id, "
                        "d.instruction_text, d.cwd, "
                        "(SELECT COUNT(*) FROM attempts a WHERE a.task_id=t.task_id) "
                        "AS attempt_count, "
                        "(SELECT a.attempt_id FROM attempts a WHERE a.task_id=t.task_id "
                        " ORDER BY a.attempt_number DESC LIMIT 1) AS last_attempt_id, "
                        "(SELECT a.agent_id FROM attempts a WHERE a.task_id=t.task_id "
                        " ORDER BY a.attempt_number DESC LIMIT 1) AS last_agent_id, "
                        "(SELECT c.session_ref_id FROM backend_calls c "
                        " WHERE c.task_id=t.task_id ORDER BY c.requested_at DESC LIMIT 1) "
                        "AS last_session_ref_id, "
                        "(SELECT c.backend FROM backend_calls c WHERE c.task_id=t.task_id "
                        " ORDER BY c.requested_at DESC LIMIT 1) AS last_backend, "
                        "(SELECT a.model FROM attempts at JOIN agent_instances a "
                        " ON a.agent_id=at.agent_id WHERE at.task_id=t.task_id "
                        " ORDER BY at.attempt_number DESC LIMIT 1) AS last_model, "
                        "(SELECT a.provider_id FROM attempts at JOIN agent_instances a "
                        " ON a.agent_id=at.agent_id WHERE at.task_id=t.task_id "
                        " ORDER BY at.attempt_number DESC LIMIT 1) AS last_provider_id "
                        "FROM tasks t JOIN task_dispatch_specs d ON d.task_id=t.task_id "
                        "WHERE t.run_id=? "
                        "ORDER BY created_at, task_id",
                        (run_id,),
                    ).fetchall()
                )
                for item in payload:
                    item["display_instruction"] = _human_task_prompt(
                        str(item.get("instruction_text") or "")
                    )
            elif resource == "models":
                run = store.connection.execute(
                    "SELECT team_id FROM runs WHERE run_id=?", (run_id,)
                ).fetchone()
                if run is None:
                    self._send_error_json(404, "run not found")
                    return
                from orchestrator.console.settings import list_saved_teams
                from orchestrator.core.config import load_team_spec

                team_id = str(run["team_id"] or "")
                team = next(
                    (
                        item
                        for item in list_saved_teams(self.server.project_root)
                        if str(item.get("team_id") or "") == team_id
                    ),
                    None,
                )
                if team is None:
                    team = next(
                        (
                            item
                            for item in list_saved_teams(self.server.project_root)
                            if item.get("source") != "saved"
                        ),
                        None,
                    )
                options: list[dict[str, str]] = []
                if team and team.get("path"):
                    try:
                        spec = load_team_spec(Path(str(team["path"])))
                    except Exception:
                        spec = None
                    if spec is not None:
                        seen: set[tuple[str, str]] = set()
                        for pool in spec.agent_pools:
                            if pool.backend not in {"codex", "codebuddy"}:
                                continue
                            if not pool.model:
                                continue
                            key = (pool.backend, pool.model)
                            if key not in seen:
                                seen.add(key)
                                options.append(
                                    {
                                        "backend": pool.backend,
                                        "model": pool.model,
                                        "provider_id": pool.provider_id,
                                    }
                                )
                payload = options
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
            elif resource == "plans":
                payload = store.list_supervisor_plans(run_id)
            elif resource == "artifacts":
                payload = store.list_artifacts(run_id)
            elif resource == "handoffs":
                payload = store.list_handoffs(run_id)
                for item in payload:
                    item["package"] = json.loads(str(item["package_json"]))
            elif resource == "results":
                payload = store.list_task_results(run_id)
            elif resource == "evidence":
                payload = store.list_verification_evidence(run_id)
            elif resource == "deliveries":
                payload = store.list_message_deliveries(run_id)
            elif resource == "progress":
                payload = _rows(
                    store.connection.execute(
                        """
                        SELECT * FROM progress_heartbeats
                        WHERE run_id=? ORDER BY observed_at, heartbeat_id
                        """,
                        (run_id,),
                    ).fetchall()
                )
            elif resource == "budget-reservations":
                payload = store.list_budget_reservations(run_id)
            elif resource == "workspace":
                row = store.connection.execute(
                    "SELECT * FROM run_workspaces WHERE run_id=?", (run_id,)
                ).fetchone()
                payload = None if row is None else dict(row)
            elif resource == "chat":
                payload = self._chat_payload(
                    run_id,
                    cursor=int((query.get("cursor") or ["0"])[0] or 0),
                    agent_id=str((query.get("agent_id") or [""])[0] or "").strip() or None,
                )
            elif resource == "worktree":
                cached = self.server._run_worktrees.get(run_id)
                if cached is not None:
                    payload = {**cached, "exists": True}
                else:
                    path = self.server.project_root / ".agent-hub" / "worktrees" / run_id
                    payload = {
                        "run_id": run_id,
                        "worktree": str(path),
                        "base_commit": None,
                        "created": False,
                        "exists": path.is_dir(),
                    }
            else:
                self._send_error_json(404, f"unknown resource {resource}")
                return
        except Exception as exc:  # noqa: BLE001
            self._send_error_json(500, type(exc).__name__)
            return
        self._send_json({"run_id": run_id, resource: payload})

    def _chat_payload(
        self,
        run_id: str,
        *,
        cursor: int = 0,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Build an isolated project → role → Agent conversation view.

        ``cursor`` is an event rowid watermark. The first R6 UI still renders
        the complete selected task, but the watermark is stable and lets a
        later client request incremental refreshes without inventing stream
        events the backend did not provide.
        """
        store = self.server.store
        run_row = store.connection.execute(
            "SELECT team_id FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        project = {
            "project_id": "current",
            "name": self.server.project_root.name or str(self.server.project_root),
            "workspace": str(self.server.project_root.resolve()),
            "team_id": str(run_row["team_id"] if run_row is not None else ""),
        }
        next_cursor = int(
            store.connection.execute(
                "SELECT COALESCE(MAX(rowid), 0) FROM events WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        )
        event_rows = store.connection.execute(
            "SELECT rowid, event_id, task_id, attempt_id, kind, from_state, to_state, "
            "data_json, created_at FROM events WHERE run_id=? AND rowid>? "
            "ORDER BY rowid LIMIT 200",
            (run_id, max(0, int(cursor))),
        ).fetchall()
        task_rows = store.connection.execute(
            """
            SELECT t.task_id, t.state, t.access_mode, t.created_at,
                   t.parent_task_id, t.dispatch_source, t.required_delivery,
                   t.task_kind,
                   d.required_role_id, d.instruction_text, d.cwd
            FROM tasks t
            JOIN task_dispatch_specs d ON d.task_id = t.task_id
            WHERE t.run_id = ?
              AND (
                ? IS NULL OR EXISTS(
                    SELECT 1 FROM attempts ax
                    WHERE ax.task_id=t.task_id AND ax.agent_id=?
                )
              )
            ORDER BY t.created_at, t.task_id
            """,
            (run_id, agent_id, agent_id),
        ).fetchall()
        result: list[dict[str, Any]] = []
        agents: dict[str, dict[str, Any]] = {}

        def decode(raw: str | None) -> dict[str, Any]:
            if not raw:
                return {}
            try:
                value = json.loads(raw)
            except (TypeError, ValueError):
                return {"text": str(raw)}
            return value if isinstance(value, dict) else {"text": str(value)}

        for task in task_rows:
            task_id = str(task["task_id"])
            messages: list[dict[str, Any]] = [{
                "id": f"task:{task_id}",
                "role": "user",
                "kind": "task",
                "agent_id": None,
                "backend": None,
                "state": "submitted",
                "text": _human_task_prompt(str(task["instruction_text"] or "")),
                "created_at": str(task["created_at"] or ""),
            }]
            protocol_rows = store.connection.execute(
                "SELECT message_id, envelope_json, created_at FROM messages "
                "WHERE run_id=? AND task_id=? ORDER BY created_at, message_id",
                (run_id, task_id),
            ).fetchall()
            for row in protocol_rows:
                envelope = decode(row["envelope_json"])
                if agent_id and str(envelope.get("target_agent_id") or "") not in {"", agent_id} \
                        and str(envelope.get("sender_agent_id") or "") != agent_id:
                    continue
                payload = envelope.get("payload")
                if isinstance(payload, dict):
                    message_text = str(payload.get("text") or payload.get("message") or payload)
                else:
                    message_text = str(payload or envelope.get("body") or envelope)
                message_role = "user" if str(envelope.get("source") or "") == "user" else "system"
                messages.append({
                    "id": str(row["message_id"]),
                    "role": message_role,
                    "kind": str(envelope.get("kind") or "message"),
                    "agent_id": envelope.get("sender_agent_id"),
                    "backend": None,
                    "state": "persisted",
                    "text": message_text,
                    "created_at": str(row["created_at"] or ""),
                })
            calls = store.connection.execute(
                """
                SELECT c.call_id, c.state, c.backend, c.requested_at,
                       c.started_at, c.finished_at, c.result_json, c.failure_json,
                       c.agent_id, a.model, a.provider_id
                FROM backend_calls c
                LEFT JOIN agent_instances a ON a.agent_id = c.agent_id
                WHERE c.run_id=? AND c.task_id=?
                  AND (? IS NULL OR c.agent_id=?)
                ORDER BY c.requested_at, c.call_id
                """,
                (run_id, task_id, agent_id, agent_id),
            ).fetchall()
            for call in calls:
                result_json = decode(call["result_json"])
                failure_json = decode(call["failure_json"])
                text = _human_result_text(
                    result_json,
                    failure_json,
                    role_id=str(task["required_role_id"] or ""),
                )
                call_agent_id = str(call["agent_id"] or "")
                if call_agent_id:
                    agents[call_agent_id] = {
                        **agents.get(call_agent_id, {}),
                        "agent_id": call_agent_id,
                        "role_id": str(task["required_role_id"] or "unassigned"),
                        "backend": call["backend"],
                        "model": call["model"],
                        "provider_id": call["provider_id"],
                    }
                messages.append({
                    "id": str(call["call_id"]),
                    "role": "agent",
                    "kind": "backend_call",
                    "agent_id": call_agent_id,
                    "backend": call["backend"],
                    "model": call["model"],
                    "provider_id": call["provider_id"],
                    "state": call["state"],
                    "text": str(text),
                    "structured": result_json.get("structured") or {},
                    "created_at": str(
                        call["finished_at"] or call["started_at"] or call["requested_at"]
                    ),
                })
            result.append({
                "task_id": task_id,
                "state": task["state"],
                "access_mode": task["access_mode"],
                "parent_task_id": task["parent_task_id"],
                "dispatch_source": task["dispatch_source"],
                "task_kind": task["task_kind"],
                "required_delivery": bool(task["required_delivery"]),
                "role_id": task["required_role_id"],
                "prompt": _human_task_prompt(str(task["instruction_text"] or "")),
                "messages": messages,
                "handoffs": store.list_handoffs(run_id, task_id=task_id),
                "results": store.list_task_results(run_id, task_id=task_id),
            })
        role_rows = store.connection.execute(
            """
            SELECT b.role_id, b.agent_id, a.backend, a.model, a.provider_id, a.status
            FROM role_bindings b JOIN agent_instances a ON a.agent_id=b.agent_id
            WHERE b.run_id=? AND b.status='ACTIVE' AND b.binding_kind='PRIMARY'
            ORDER BY b.role_id, b.agent_id
            """,
            (run_id,),
        ).fetchall()
        roles: dict[str, dict[str, Any]] = {}
        try:
            snapshot = store.run_snapshot(run_id).get("snapshot") or {}
        except (KeyError, TypeError):
            snapshot = {}
        role_titles = {
            str(item.get("role_id")): str(item.get("title") or item.get("role_id"))
            for item in snapshot.get("roles", [])
            if isinstance(item, dict) and item.get("role_id")
        }
        for row in role_rows:
            agent_id_value = str(row["agent_id"])
            agent = {
                "agent_id": agent_id_value,
                "role_id": str(row["role_id"]),
                "backend": row["backend"],
                "model": row["model"],
                "provider_id": row["provider_id"],
                "status": row["status"],
            }
            # Include idle configured agents as well as agents that already
            # made a backend call, so every role has its own chat context.
            agents[agent_id_value] = agent
            role = roles.setdefault(
                str(row["role_id"]),
                {
                    "role_id": str(row["role_id"]),
                    "title": role_titles.get(str(row["role_id"]), str(row["role_id"])),
                    "agents": [],
                },
            )
            role["agents"].append(agent)
        return {
            "project": project,
            "roles": list(roles.values()),
            "tasks": result,
            "agents": list(agents.values()),
            "events": [
                {
                    "cursor": int(row["rowid"]),
                    "event_id": row["event_id"],
                    "task_id": row["task_id"],
                    "attempt_id": row["attempt_id"],
                    "kind": row["kind"],
                    "from_state": row["from_state"],
                    "to_state": row["to_state"],
                    "data": decode(row["data_json"]),
                    "created_at": row["created_at"],
                }
                for row in event_rows
            ],
            "cursor": next_cursor,
            "requested_cursor": cursor,
            "incremental": cursor > 0,
        }

    # -- 协调写操作 ---------------------------------------------------------

    def _with_control(
        self,
        run_id: str,
        fn: Callable[[Any, Any], None],
        *,
        allow_active_serve: bool = False,
    ) -> bool:
        """执行协调写操作；审核/删除可复用 serve 当前 fencing token。"""
        store = self.server.store
        controller = store.acquire_run_controller(
            run_id, "console-op", lease_seconds=60
        )
        owns_controller = controller is not None
        if controller is None and allow_active_serve:
            active = store.active_run_controller(run_id)
            authority_row = store.active_authority(run_id)
            if (
                active is not None
                and str(active["owner_id"]).startswith("serve-")
                and authority_row is not None
                and str(authority_row["owner_agent_id"]) == str(active["owner_id"])
            ):
                controller = ControllerToken(
                    run_id=run_id,
                    owner_id=str(active["owner_id"]),
                    epoch=int(active["epoch"]),
                    expires_at=str(active["expires_at"]),
                )
                authority = AuthorityToken(
                    run_id=run_id,
                    owner_agent_id=str(authority_row["owner_agent_id"]),
                    role_id=str(authority_row["role_id"]),
                    epoch=int(authority_row["epoch"]),
                    expires_at=str(authority_row["expires_at"]),
                )
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
            if owns_controller and controller is not None:
                store.release_run_controller(controller)

    def _post_run_action(self, path: str, body: dict[str, Any]) -> None:
        parts = path.strip("/").split("/")
        if len(parts) < 3 or parts[0] != "api" or parts[1] != "runs":
            self._send_error_json(404, "not found")
            return
        run_id = parts[2]
        action = parts[3] if len(parts) > 3 else ""
        store = self.server.store
        if action == "plans" and len(parts) >= 6 and parts[5] in {"approve", "reject"}:
            self._review_plan(run_id, parts[4], parts[5], body)
            return
        if action == "tasks" and len(parts) >= 6 and parts[5] == "cancel":
            task_id = parts[4]
            self._with_control(
                run_id,
                lambda c, a: store.request_cancel_task(
                    task_id, c, reason=str(body.get("reason") or "console-cancel")
                ),
            ) and self._send_json({"ok": True, "task_id": task_id})
            return
        if action == "tasks" and len(parts) >= 6 and parts[5] == "delete":
            task_id = parts[4]
            self._with_control(
                run_id,
                lambda c, a: store.delete_task(
                    run_id,
                    task_id,
                    c,
                    a,
                    reason=str(body.get("reason") or "console-delete"),
                ),
                allow_active_serve=True,
            ) and self._send_json({"ok": True, "task_id": task_id, "deleted": True})
            return
        if action == "tasks" and len(parts) >= 6 and parts[5] == "review":
            task_id = parts[4]
            self._review_task(run_id, task_id, body)
            return
        if action == "tasks" and len(parts) >= 6 and parts[5] == "guidance":
            self._queue_guidance(run_id, parts[4], body)
            return
        if action == "tasks" and len(parts) == 4:
            self._create_task(run_id, body)
            return
        if action == "worktree":
            self._prepare_worktree(run_id)
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

    def _queue_guidance(self, run_id: str, task_id: str, body: dict[str, Any]) -> None:
        """Persist a user hint; serve decides whether the backend can apply it now."""
        text = str(body.get("text") or body.get("prompt") or "").strip()
        if not text:
            self._send_error_json(400, "guidance text must not be empty")
            return
        if len(text) > 20_000:
            self._send_error_json(400, "guidance text is too long")
            return
        store = self.server.store
        row = store.connection.execute(
            """
            SELECT r.team_id, t.state, a.attempt_id, a.agent_id, a.state AS attempt_state,
                   a.generation
            FROM tasks t JOIN runs r ON r.run_id=t.run_id
            LEFT JOIN attempts a ON a.attempt_id=(
                SELECT ax.attempt_id FROM attempts ax
                WHERE ax.task_id=t.task_id ORDER BY ax.attempt_number DESC LIMIT 1
            )
            WHERE t.run_id=? AND t.task_id=?
            """,
            (run_id, task_id),
        ).fetchone()
        if row is None:
            self._send_error_json(404, "task not found")
            return
        if row["attempt_id"] is None or row["attempt_state"] not in {
            "ASSIGNED", "RUNNING", "CANCEL_REQUESTED"
        }:
            self._send_error_json(
                409,
                "task has no active attempt; guidance must target a running Agent",
            )
            return
        from orchestrator.core.models import MessageEnvelope, Recipient

        plan_row = store.connection.execute(
            "SELECT MAX(revision) AS revision FROM supervisor_plans WHERE run_id=?",
            (run_id,),
        ).fetchone()
        message = MessageEnvelope(
            message_id=f"msg-guidance-{uuid.uuid4().hex[:16]}",
            team_id=str(row["team_id"]),
            run_id=run_id,
            task_id=task_id,
            sender_agent_id="user",
            recipients=(Recipient("agent", str(row["agent_id"])),),
            kind="user_guidance",
            message_type="user_guidance",
            source="user",
            target_agent_id=str(row["agent_id"]),
            attempt_id=str(row["attempt_id"]),
            plan_revision=(int(plan_row["revision"]) if plan_row and plan_row["revision"] else None),
            payload={"text": text},
            correlation_id=f"guidance:{task_id}:{row['attempt_id']}",
            idempotency_key=(
                f"guidance:{task_id}:{row['attempt_id']}:"
                f"{hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]}"
            ),
            expires_at=(
                datetime.now(timezone.utc) + timedelta(minutes=30)
            ).isoformat(),
        )
        try:
            store.append_message(message)
        except Exception as exc:  # noqa: BLE001
            self._send_error_json(400, f"{type(exc).__name__}: {exc}")
            return
        self._send_json(
            {
                "ok": True,
                "status": "queued",
                "message_id": message.message_id,
                "task_id": task_id,
                "attempt_id": row["attempt_id"],
                "agent_id": row["agent_id"],
            }
        )

    def _create_task(self, run_id: str, body: dict[str, Any]) -> None:
        if TaskState is None:  # pragma: no cover
            self._send_error_json(500, "TaskState unavailable")
            return
        mode = str(body.get("mode") or "worker")
        if mode not in {"worker", "supervisor"}:
            self._send_error_json(400, "mode must be worker or supervisor")
            return
        task_id = str(body.get("task_id") or f"task-{uuid.uuid4().hex[:12]}")
        access_mode = (
            "read_only" if mode == "supervisor"
            else str(body.get("access_mode") or "read_only")
        )
        write_scope = () if mode == "supervisor" else tuple(body.get("write_scope") or ())
        prompt = str(body.get("prompt") or task_id)
        cwd = str(body.get("cwd") or self.server.worktree or ".")
        timeout = float(body.get("timeout_seconds") or 60)
        required_role_id = (
            "supervisor" if mode == "supervisor"
            else str(body.get("required_role_id") or "worker")
        )
        required_backend_raw = body.get("required_backend")
        required_backend_text = "" if required_backend_raw is None else str(required_backend_raw)
        required_backend = required_backend_text.strip() or None
        required_model_raw = body.get("required_model")
        required_model_text = "" if required_model_raw is None else str(required_model_raw)
        required_model = required_model_text.strip() or None
        required_provider_raw = body.get("required_provider_id")
        required_provider_text = (
            "" if required_provider_raw is None else str(required_provider_raw)
        )
        required_provider_id = required_provider_text.strip() or None
        if mode == "worker" and required_role_id == "supervisor":
            self._send_error_json(
                400, "supervisor tasks must use mode=supervisor"
            )
            return
        if mode == "supervisor":
            try:
                from orchestrator.console.settings import list_saved_teams
                from orchestrator.core.config import load_team_spec
                from orchestrator.core.role_registry import build_supervisor_plan_prompt

                run_row = self.server.store.connection.execute(
                    "SELECT team_id FROM runs WHERE run_id=?", (run_id,)
                ).fetchone()
                selected = next(
                    (item for item in list_saved_teams(self.server.project_root)
                     if run_row is not None and str(item.get("team_id") or "") == str(run_row["team_id"])),
                    None,
                )
                if selected and selected.get("path"):
                    prompt = build_supervisor_plan_prompt(
                        load_team_spec(Path(str(selected["path"]))),
                        user_goal=prompt,
                        budget={"max_tasks": 8, "max_worker_concurrency": 2},
                    )
            except Exception:
                # Legacy or manually-created Runs can still submit a plain
                # prompt; local validation remains authoritative.
                pass
        try:
            self.server.store.create_task(
                run_id,
                task_id,
                access_mode=access_mode,
                write_scope=write_scope,
                required_role_id=required_role_id,
                required_backend=required_backend,
                required_model=required_model,
                required_provider_id=required_provider_id,
                prompt=prompt,
                cwd=cwd,
                timeout_seconds=timeout,
            )
            self.server.store.transition_task(task_id, TaskState.READY, reason="console")
        except Exception as exc:  # noqa: BLE001
            self._send_error_json(400, f"{type(exc).__name__}: {exc}")
            return
        self._send_json({"ok": True, "task_id": task_id, "mode": mode})

    # -- 写任务闭环 ---------------------------------------------------------

    def _prepare_worktree(self, run_id: str) -> None:
        """幂等准备 run 的受管 worktree（写任务 cwd 必须落在这里）。"""
        try:
            info = self.server.ensure_run_worktree(run_id)
        except Exception as exc:  # noqa: BLE001
            self._send_error_json(400, f"{type(exc).__name__}: {exc}")
            return
        self._send_json({"ok": True, **info})

    def _review_task(
        self, run_id: str, task_id: str, body: dict[str, Any]
    ) -> None:
        """REVIEW 人工审核：approve=通过（write 产出 commit→入队→集成→COMPLETED；
        read 直接 COMPLETED）；rework=打回（记录 REWORK + reassign 重新派发）。"""
        decision = str(body.get("decision") or "")
        if decision not in {"approve", "rework"}:
            self._send_error_json(400, "decision must be approve or rework")
            return
        comment = str(body.get("comment") or "")

        def run(controller: Any, authority: Any) -> None:
            store = self.server.store
            task = store.connection.execute(
                "SELECT state, access_mode, write_scope_json FROM tasks "
                "WHERE task_id=? AND run_id=?",
                (task_id, run_id),
            ).fetchone()
            if task is None:
                raise KeyError(task_id)
            if str(task["state"]) != "REVIEW":
                raise ValueError(f"task {task_id} is {task['state']}, must be REVIEW")
            attempt = store.connection.execute(
                "SELECT attempt_id, workspace_path, base_commit FROM attempts WHERE task_id=? "
                "ORDER BY attempt_number DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            attempt_id = str(attempt["attempt_id"]) if attempt is not None else None
            detail = {"comment": comment, "decided_by": "console"}
            if decision == "rework":
                store.record_review_decision(
                    run_id, task_id, attempt_id=attempt_id, layer="human",
                    decision="REWORK", decided_by="console", detail=detail,
                    authority=authority,
                )
                store.reassign_task(
                    run_id, task_id, controller, authority, reason="console-rework"
                )
                return
            if str(task["access_mode"]) == "read_only":
                # 只读任务无 git 产出：直接终态（REVIEW -> COMPLETED 合法）
                store.record_review_decision(
                    run_id, task_id, attempt_id=attempt_id, layer="human",
                    decision="APPROVED", decided_by="console", detail=detail,
                    authority=authority,
                )
                store.transition_task(task_id, TaskState.COMPLETED, reason="console-approve")
                return
            # 写任务：worktree 产出 commit -> 入队 -> 真实集成
            if attempt_id is None:
                raise ValueError("no attempt found to settle")
            from orchestrator.workspace.merge_executor import MergeExecutor

            info = self.server.ensure_run_worktree(run_id)
            if (
                attempt is not None
                and attempt["workspace_path"]
                and getattr(store, "workspace_manager", None) is not None
            ):
                worktree = Path(str(attempt["workspace_path"]))
                workspace_manager = store.workspace_manager
                git_manager = workspace_manager._git_manager_for(
                    run_id, worktree.parent
                )
                git_manager.adopt_worktree(worktree)
                info = {
                    "run_id": run_id,
                    "worktree": str(worktree),
                    "base_commit": str(attempt["base_commit"] or info["base_commit"]),
                }
            else:
                worktree = Path(info["worktree"])
            scope = json.loads(task["write_scope_json"] or "[]")
            commit_manager = (
                git_manager
                if "git_manager" in locals()
                else self.server.git_manager()
            )
            result_commit = commit_manager.commit_managed_changes(
                worktree, tuple(scope), f"console approve {task_id}"
            )
            run_snapshot = store.run_snapshot(run_id)
            if run_snapshot.get("snapshot") is not None:
                from orchestrator.verification import VerificationService

                requested_checks = body.get("checks")
                checks = (
                    tuple(str(item) for item in requested_checks)
                    if isinstance(requested_checks, list) and requested_checks
                    else ("commit_exists", "diff_check")
                )
                evidence = VerificationService(store).verify_candidate(
                    run_id, task_id, attempt_id, result_commit, checks=checks
                )
                if any(item.status != "PASS" for item in evidence):
                    raise ValueError("candidate verification did not pass; inspect /evidence")
                detail["candidate_commit"] = result_commit
                detail["evidence_refs"] = [item.evidence_id for item in evidence]
            store.record_review_decision(
                run_id, task_id, attempt_id=attempt_id, layer="human",
                decision="APPROVED", decided_by="console", detail=detail,
                authority=authority,
            )
            store.enqueue_merge(
                run_id, task_id, attempt_id, result_commit,
                str(info["base_commit"]), controller, authority=authority,
                reason="console-review-approved",
            )
            result = MergeExecutor(store, self.server.git_manager()).run_merge_once(
                run_id, controller, authority
            )
            if result.get("status") == "busy":
                raise ValueError("merge queue busy; retry")

        if self._with_control(run_id, run, allow_active_serve=True):
            self._send_json({"ok": True, "decision": decision})

    def _review_plan(
        self, run_id: str, supervisor_task_id: str, action: str, body: dict[str, Any]
    ) -> None:
        """Approve/return a persisted supervisor plan preview."""
        try:
            revision = int(body.get("revision"))
        except (TypeError, ValueError):
            self._send_error_json(400, "revision must be an integer")
            return
        digest = str(body.get("plan_digest") or body.get("digest") or "").strip()
        if not digest:
            self._send_error_json(400, "plan_digest is required")
            return

        def run(controller: Any, authority: Any) -> None:
            if action == "approve":
                store = self.server.store
                store.approve_supervisor_plan(
                    run_id,
                    supervisor_task_id,
                    revision,
                    digest,
                    controller=controller,
                    authority=authority,
                    permissions=body.get("permissions"),
                )
                # Approval and materialization share the same fencing tokens;
                # the next serve tick remains an idempotent safety net.
                from orchestrator.console.settings import list_saved_teams
                from orchestrator.core.config import load_team_spec
                from orchestrator.core.supervisor_planning import materialize_ready_supervisor_plans

                run_row = store.connection.execute(
                    "SELECT team_id FROM runs WHERE run_id=?", (run_id,)
                ).fetchone()
                if run_row is None:
                    raise KeyError(run_id)
                team = next(
                    (item for item in list_saved_teams(self.server.project_root)
                     if str(item.get("team_id") or "") == str(run_row["team_id"])),
                    None,
                )
                if team is None or not team.get("path"):
                    raise ValueError("saved team configuration is required to approve a plan")
                spec = load_team_spec(Path(str(team["path"])))
                materialize_ready_supervisor_plans(
                    store,
                    run_id=run_id,
                    team_spec=spec,
                    controller=controller,
                    authority=authority,
                )
            else:
                store = self.server.store
                row = store.connection.execute(
                    "SELECT plan_id, status, plan_digest FROM supervisor_plans "
                    "WHERE run_id=? AND supervisor_task_id=? AND revision=?",
                    (run_id, supervisor_task_id, revision),
                ).fetchone()
                if row is None:
                    raise KeyError("plan revision not found")
                if str(row["plan_digest"]) != digest:
                    raise ValueError("plan digest does not match the selected revision")
                with store.connection:
                    store.connection.execute(
                        "UPDATE supervisor_plans SET status='needs_revision', updated_at=? WHERE plan_id=?",
                        (utc_now(), row["plan_id"]),
                    )
                    store._append_event(
                        run_id, supervisor_task_id, None, "plan.needs_revision",
                        "REVIEW", "REVIEW",
                        {"revision": revision, "plan_digest": digest, "reason": str(body.get("reason") or "operator returned plan")[:500]},
                    )

        if self._with_control(run_id, run, allow_active_serve=True):
            self._send_json({"ok": True, "run_id": run_id, "task_id": supervisor_task_id, "revision": revision, "decision": action})

    def _serve_action(self, run_id: str, action: str, body: dict[str, Any]) -> None:
        manager = self.server.serve_manager
        if action == "start":
            self.server._serve_stopped.discard(run_id)
            requested_path = str(body.get("team_path") or "").strip()
            team_id = str(body.get("team_id") or "").strip()
            run_team_id = ""
            run_row = self.server.store.connection.execute(
                "SELECT team_id FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if run_row is None:
                self._send_error_json(404, f"run not found: {run_id}")
                return
            run_team_id = str(run_row["team_id"] or "").strip()
            team_id = team_id or run_team_id
            if not requested_path:
                from orchestrator.console.settings import list_saved_teams

                teams = list_saved_teams(self.server.project_root)
                selected = next(
                    (
                        item
                        for item in teams
                        if str(item.get("team_id") or "") == team_id
                    ),
                    None,
                )
                # Keep compatibility with Runs created before the default
                # team started exposing its real team_id.
                if selected is None and team_id == "default":
                    selected = next(
                        (
                            item
                            for item in teams
                            if str(item.get("source") or "") != "saved"
                        ),
                        None,
                    )
                if selected is None or not selected.get("path"):
                    self._send_error_json(
                        400,
                        f"team {team_id or '<empty>'} is not saved; select a saved team before starting serve",
                    )
                    return
                requested_path = str(selected["path"])
                team_id = str(selected.get("team_id") or team_id)
                if run_team_id != team_id:
                    if run_team_id == "default":
                        # Runs created before the default team exposed its
                        # real ID need a one-time migration; otherwise the
                        # serve process would register agents under a team
                        # that the Run does not reference.
                        with self.server.store.connection:
                            self.server.store.connection.execute(
                                "UPDATE runs SET team_id=? WHERE run_id=?",
                                (team_id, run_id),
                            )
                    else:
                        self._send_error_json(
                            400,
                            f"run team {run_team_id} does not match saved team {team_id}",
                        )
                        return
            team_path = Path(requested_path)
            if not team_path.is_absolute():
                team_path = self.server.project_root / team_path
            if not team_path.is_file():
                self._send_error_json(400, f"team file not found: {team_path}")
                return
            from orchestrator.core.config import load_team_spec

            try:
                actual_team_id = load_team_spec(team_path).team_id
            except Exception as exc:  # noqa: BLE001 - return a safe config error
                self._send_error_json(
                    400, f"invalid team file {team_path}: {type(exc).__name__}"
                )
                return
            if run_team_id and actual_team_id != run_team_id:
                if run_team_id == "default":
                    with self.server.store.connection:
                        self.server.store.connection.execute(
                            "UPDATE runs SET team_id=? WHERE run_id=?",
                            (actual_team_id, run_id),
                        )
                else:
                    self._send_error_json(
                        400,
                        f"run team {run_team_id} does not match team file {actual_team_id}",
                    )
                    return
            team_id = actual_team_id
            result = manager.start(
                run_id, team_path, db_path=self.server.db_path
            )
            result = dict(result)
            result.update({"team_id": team_id, "team_path": str(team_path)})
            self._send_json(result)
            return
        if action == "stop":
            result = manager.stop(run_id)
            self.server._serve_stopped.add(run_id)
            self._send_json(result)
            return
        if action == "status":
            self._send_json(manager.status(run_id))
            return
        self._send_error_json(404, "unknown serve action")

    def _create_run(self, body: dict[str, Any]) -> None:
        run_id = str(body.get("run_id") or f"run-{uuid.uuid4().hex[:12]}")
        team_id = str(body.get("team_id") or "default")
        approval_mode = str(body.get("approval_mode") or "manual")
        team_spec = None
        try:
            from orchestrator.console.settings import list_saved_teams
            from orchestrator.core.config import load_team_spec

            requested_path = str(body.get("team_path") or "").strip()
            selected = None
            teams = list_saved_teams(self.server.project_root)
            if requested_path:
                selected_path = Path(requested_path)
                if not selected_path.is_absolute():
                    selected_path = self.server.project_root / selected_path
                team_spec = load_team_spec(selected_path)
            else:
                selected = next(
                    (item for item in teams if str(item.get("team_id") or "") == team_id),
                    None,
                )
                if selected is None and team_id == "default":
                    selected = next(
                        (item for item in teams if item.get("source") != "saved"), None
                    )
                if selected and selected.get("path"):
                    team_spec = load_team_spec(Path(str(selected["path"])))
            if team_spec is not None:
                team_id = str(team_spec.team_id)
        except Exception as exc:  # noqa: BLE001 - team snapshots are optional for legacy callers
            if body.get("team_path"):
                self._send_error_json(400, f"{type(exc).__name__}: invalid team")
                return
        try:
            self.server.store.create_run(
                run_id,
                team_id,
                team_spec=team_spec,
                approval_mode=approval_mode,
            )
        except Exception as exc:  # noqa: BLE001
            self._send_error_json(409, f"{type(exc).__name__}: {exc}")
            return
        snapshot = self.server.store.run_snapshot(run_id)
        self._send_json({"ok": True, "run_id": run_id, "team_id": team_id, **(snapshot or {})})

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
        self._git_manager: Any | None = None
        self._run_worktrees: dict[str, dict[str, Any]] = {}
        self._serve_stopped: set[str] = set()
        super().__init__((host, port), ConsoleHandler)

    def close(self) -> None:
        self.serve_manager.shutdown_all()
        self.server_close()

    # -- 写任务闭环：受管 worktree ------------------------------------------

    def git_manager(self) -> Any:
        """懒创建受管 GitWorkspaceManager（项目仓库为受管主仓库）。"""
        if self._git_manager is None:
            from orchestrator.workspace.git_manager import GitWorkspaceManager

            self._git_manager = GitWorkspaceManager(
                self.project_root,
                self.project_root / ".agent-hub" / "worktrees",
            )
        return self._git_manager

    def ensure_run_worktree(self, run_id: str) -> dict[str, Any]:
        """幂等：为 run 准备受管 worktree（存在则复用）。

        返回 {run_id, worktree, base_commit, created}。首次创建前把项目仓库
        标记为受管（agenthub.managed=true，幂等 git config）。
        """
        cached = self._run_worktrees.get(run_id)
        if cached is not None:
            return {**cached, "created": False}
        import subprocess

        manager = self.git_manager()
        base = manager.head(self.project_root)
        worktree = manager.worktrees_root / run_id
        created = False
        if not worktree.is_dir():
            # 主仓库标记受管（幂等）；已标记时忽略失败
            subprocess.run(
                ["git", "-C", str(self.project_root), "config",
                 "agenthub.managed", "true"],
                check=False,
            )
            # 写任务 worktree 建在项目内 .agent-hub/ 下；若该目录未被忽略，
            # 注入仓库本地 exclude（.git/info/exclude），保证集成前
            # 「integration repository must be clean」检查不被 untracked 干扰。
            self._exclude_agent_hub()
            worktree = manager.create_worktree(run_id, base)
            created = True
        info: dict[str, Any] = {
            "run_id": run_id,
            "worktree": str(worktree),
            "base_commit": base,
            "created": created,
        }
        self._run_worktrees[run_id] = info
        return dict(info)

    def _exclude_agent_hub(self) -> None:
        """仓库本地忽略 .agent-hub/（.git/info/exclude，幂等，不改 .gitignore）。"""
        import subprocess

        check = subprocess.run(
            ["git", "-C", str(self.project_root), "check-ignore",
             ".agent-hub/"],
            capture_output=True,
            check=False,
        )
        if check.returncode == 0:
            return  # 已被忽略（.gitignore 或已有 exclude）
        exclude = self.project_root / ".git" / "info" / "exclude"
        try:
            text = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
        except OSError:
            return
        if ".agent-hub/" not in text:
            exclude.parent.mkdir(parents=True, exist_ok=True)
            exclude.write_text(
                (text.rstrip() + "\n" if text.strip() else "")
                + ".agent-hub/\n",
                encoding="utf-8",
            )


def find_free_port(host: str, start: int, tries: int = 20) -> int | None:
    """返回 >= start 的第一个可绑定端口；范围内全部被占用时返回 None。

    本地单用户工具的现实动机：Steam 等软件常占用 8080，把 requested
    端口让给此类进程、自己顺延，可避免"起不来"或开错页面。
    """
    for candidate in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, candidate))
                return candidate
            except OSError:
                continue
    return None


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
    from orchestrator.workspace.run_manager import RunWorkspaceManager

    store = SQLiteStateStore(
        db_path,
        workspace_manager=RunWorkspaceManager(project_root),
    )
    actual_port = find_free_port(host, port)
    if actual_port is None:
        print(f"console error: no free port in {port}..{port + 19} on {host}")
        store.close()
        return
    if actual_port != port:
        print(f"console: port {port} busy (e.g. Steam) -> using {actual_port}")
    server = ConsoleHTTPServer(
        store,
        project_root=project_root,
        db_path=db_path,
        host=host,
        port=actual_port,
        initial_run_id=initial_run_id,
        worktree=worktree,
    )
    print(
        f"console listening on http://{host}:{actual_port}  "
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


