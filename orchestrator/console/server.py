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
        data = _DEFAULT_INDEX_HTML.encode("utf-8")
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


_DEFAULT_INDEX_HTML = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>Agent Hub Console</title>
<style>
:root{color-scheme:dark}
body{font-family:ui-sans-serif,system-ui,sans-serif;margin:0;background:#0f172a;color:#e2e8f0;font-size:14px}
header{padding:12px 24px;background:#1e293b;display:flex;align-items:center;gap:24px;position:sticky;top:0;z-index:5}
h1{font-size:16px;margin:0}
nav{display:flex;gap:4px}
nav button{background:none;border:0;color:#94a3b8;padding:6px 12px;border-radius:8px;cursor:pointer;font-size:14px}
nav button.active{background:#2563eb;color:#fff}
main{max-width:1200px;margin:0 auto;padding:24px;display:none}
main.active{display:block}
section{margin-bottom:20px}
h2{font-size:14px;color:#94a3b8;text-transform:uppercase;letter-spacing:.05em}
table{width:100%;border-collapse:collapse}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid #334155;vertical-align:top}
th{color:#94a3b8;font-weight:600}
.badge{display:inline-block;padding:2px 8px;border-radius:9999px;font-size:11px}
.badge-ok{background:#166534;color:#86efac}.badge-warn{background:#7c2d12;color:#fdba74}
.badge-off{background:#1e293b;color:#64748b}
button{background:#2563eb;color:#fff;border:0;padding:6px 12px;border-radius:6px;cursor:pointer}
button.ghost{background:transparent;border:1px solid #334155;color:#94a3b8}
button.danger{background:#7f1d1d}
button:disabled{opacity:.5;cursor:not-allowed}
form{display:flex;gap:8px;flex-wrap:wrap;align-items:end;margin-bottom:10px}
input,select{padding:6px 8px;border-radius:6px;border:1px solid #334155;background:#1e293b;color:#e2e8f0}
label{display:flex;flex-direction:column;gap:4px;font-size:12px;color:#94a3b8}
.muted{color:#64748b;font-size:12px}
.pool-row{display:grid;grid-template-columns:1fr 1fr 1fr 100px 80px auto;gap:8px;align-items:end;margin-bottom:8px}
.panel{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:14px;margin-bottom:10px}
pre{background:#0b1220;padding:10px;border-radius:8px;overflow:auto;font-size:12px;color:#7dd3fc}
.flex{display:flex;gap:8px;align-items:center}
.mono{font-family:ui-monospace,monospace;font-size:12px}
</style></head><body>
<header>
  <h1>Agent Hub Console</h1>
  <nav>
    <button data-tab="runs" class="active">Runs</button>
    <button data-tab="teams">Teams</button>
    <button data-tab="connections">Connections</button>
  </nav>
  <span id="conn" class="muted" style="margin-left:auto"></span>
</header>

<main id="pane-runs" class="active">
  <section><h2>新建 Run</h2>
    <form id="newRunForm">
      <label>run_id<input name="run_id" placeholder="留空自动生成"></label>
      <label>team
        <select name="team_id"><option value="default">默认 (config/team.yaml)</option></select>
      </label>
      <button type="submit">创建</button>
    </form>
  </section>
  <section><h2>Run 列表</h2><table id="runsTbl">
    <thead><tr><th>run</th><th>team</th><th>tasks</th><th>完成</th><th>serve</th><th>操作</th></tr></thead>
    <tbody></tbody></table></section>
  <section><h2>Run 详情 <span id="detailTitle" class="muted"></span></h2>
    <div class="flex" style="margin-bottom:8px"><button class="ghost" id="openTaskForm">+ 发起任务</button>
      <button class="ghost" id="btnPause">暂停</button><button class="ghost" id="btnResume">恢复</button></div>
    <div id="taskFormBox" style="display:none"><form id="taskForm">
      <input name="task_id" placeholder="task_id"><select name="access_mode"><option>read_only</option><option>write</option></select>
      <input name="write_scope" placeholder="write_scope，逗号分隔"><input name="prompt" placeholder="prompt" size="36">
      <input name="cwd" placeholder="cwd"><button type="submit">发起</button></form></div>
    <table id="detailTbl"><thead><tr><th>task</th><th>状态</th><th>模式</th><th>attempts</th><th></th></tr></thead>
      <tbody></tbody></table>
    <h2>审批</h2><table id="approvalsTbl"><thead><tr><th>id</th><th>task</th><th>原因</th><th>状态</th></tr></thead><tbody></tbody></table>
    <h2>Merge Queue</h2><table id="mergesTbl"><thead><tr><th>merge</th><th>task</th><th>commit</th><th>状态</th></tr></thead><tbody></tbody></table>
  </section>
</main>

<main id="pane-teams">
  <section><h2>组建临时 Team</h2>
    <div class="panel">
      <label>team_id<input id="newTeamId" value="ad-hoc-team" style="margin-bottom:8px"></label>
      <div id="poolRows"></div>
      <button id="addPool">+ 添加 Agent 池</button>
      <div style="margin-top:10px"><button id="saveTeam" class="ghost">保存到 .agent-hub/teams</button>
        <button id="saveAsDefault" class="ghost">保存为 config/team.yaml（覆盖默认）</button></div>
    </div>
    <pre id="teamPreview"></pre>
  </section>
  <section><h2>可用 Team</h2><table id="teamsTbl">
    <thead><tr><th>team</th><th>来源</th><th>Pools</th><th></th></tr></thead><tbody></tbody></table></section>
</main>

<main id="pane-connections">
  <section><h2>厂商连接与登录引导</h2>
    <div class="flex" style="margin-bottom:8px"><button id="probeBtn">刷新探测</button>
      <span class="muted">本页不存储任何 token/credential；登录请在终端执行命令后返回刷新。</span></div>
    <table id="connTbl"><thead><tr><th>backend</th><th>CLI</th><th>版本</th><th>登录命令</th><th>说明</th></tr></thead>
      <tbody></tbody></table></section>
</main>

<script>
let currentRun='';
function $(s){return document.querySelector(s);}
function esc(v){return String(v==null?'':v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
async function api(path,opt){const r=await fetch(path,opt);const d=await r.json().catch(()=>({}));return {status:r.status,body:d};}
function badge(state){const s=(state||'').toLowerCase();
 const cls=s.includes('complete')?'badge-ok':(s.includes('fail')||s.includes('cancel')||s.includes('reject'))?'badge-warn':'badge-off';
 return `<span class="badge ${cls}">${esc(state||'')}</span>`;}

// ---- tab ----
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('nav button').forEach(x=>x.classList.toggle('active',x===b));
  document.querySelectorAll('main').forEach(m=>m.classList.toggle('active',m.id==='pane-'+b.dataset.tab));
  if(b.dataset.tab==='teams')loadTeams(); if(b.dataset.tab==='connections')loadConnections();
  if(b.dataset.tab==='runs')loadRuns();
});

// ---- Runs ----
async function loadRuns(){
  const r=await api('/api/runs');
  const tb=$('#runsTbl tbody');tb.innerHTML='';
  const teamsSel=$('select[name=team_id]');
  const selOpts=new Set([...teamsSel.options].map(o=>o.value));
  const t=await api('/api/teams');
  for(const team of (t.body.teams||[])){ if(!selOpts.has(team.team_id)){
    const o=document.createElement('option');o.value=team.team_id;o.textContent=team.team_id;
    teamsSel.appendChild(o);} }
  for(const run of r.body.runs||[]){
    const tr=document.createElement('tr');
    tr.innerHTML=`<td class="mono">${esc(run.run_id)}</td><td>${esc(run.team_id)}</td>
      <td>${run.tasks_total??''}</td><td>${run.tasks_completed??''}</td>
      <td>${run.running?'<span class="badge badge-ok">running</span>':'<span class="badge badge-off">stopped</span>'}</td>
      <td><div class="flex">${run.running?'<button class="danger" data-stop="'+esc(run.run_id)+'">停止</button>':'<button data-start="'+esc(run.run_id)+'">启动 serve</button>'}
      <button class="ghost" data-open="'+esc(run.run_id)+'">详情</button></div></td>`;
    tb.appendChild(tr);
  }
  tb.querySelectorAll('[data-open]').forEach(b=>b.onclick=()=>openRun(b.dataset.open));
  tb.querySelectorAll('[data-start]').forEach(b=>b.onclick=()=>startServe(b.dataset.start));
  tb.querySelectorAll('[data-stop]').forEach(b=>b.onclick=()=>stopServe(b.dataset.stop));
}
$('#newRunForm').addEventListener('submit',async e=>{e.preventDefault();
  const f=new FormData(e.target);const body={};
  for(const[k,v]of f.entries())if(v)body[k]=v;
  const r=await api('/api/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(r.status===200){alert('created '+r.body.run_id);loadRuns();}
  else alert(r.body.error||'create failed');});
async function startServe(runId){
  const teamPath=prompt('team 配置文件路径（相对项目根）:','config/team.yaml');
  if(!teamPath)return;
  const r=await api(`/api/runs/${runId}/serve/start`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({team_path:teamPath})});
  alert(JSON.stringify(r.body));loadRuns();setTimeout(loadRuns,1500);}
async function stopServe(runId){
  await api(`/api/runs/${runId}/serve/stop`,{method:'POST',body:'{}'});
  loadRuns();}
async function openRun(runId){currentRun=runId;$('#detailTitle').textContent='· '+runId;loadDetail();}

async function loadDetail(){
  const runId=currentRun;if(!runId)return;
  const tasks=await api(`/api/runs/${runId}/tasks`);const appr=await api(`/api/runs/${runId}/approvals`);const mg=await api(`/api/runs/${runId}/merges`);
  const tb=$('#detailTbl tbody');tb.innerHTML='';
  for(const t of tasks.body.tasks||[]){const tr=document.createElement('tr');
    tr.innerHTML=`<td class="mono">${esc(t.task_id)}</td><td>${badge(t.state)}</td><td>${esc(t.access_mode)}</td>
      <td>${t.attempt_count??''}</td>
      <td><button class="ghost" data-cancel="${esc(t.task_id)}">取消</button></td>`;tb.appendChild(tr);}
  tb.querySelectorAll('[data-cancel]').forEach(b=>b.onclick=async()=>{
    const r=await api(`/api/runs/${runId}/tasks/${b.dataset.cancel}/cancel`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reason:'console-cancel'})});
    if(r.status!==200)alert(r.body.error||'cancel failed');loadDetail();});
  const at=$('#approvalsTbl tbody');at.innerHTML='';
  for(const a of appr.body.approvals||[]){const tr=document.createElement('tr');
    tr.innerHTML=`<td class="mono">${esc(a.request_id)}</td><td>${esc(a.task_id||'')}</td><td>${esc((a.action_summary||'').slice(0,50))}</td><td>${badge(a.status)}</td>`;at.appendChild(tr);}
  const mt=$('#mergesTbl tbody');mt.innerHTML='';
  for(const m of mg.body.merges||[]){const tr=document.createElement('tr');
    tr.innerHTML=`<td class="mono">${esc(m.merge_id)}</td><td>${esc(m.task_id)}</td><td>${esc((m.result_commit||'').slice(0,10))}</td><td>${badge(m.status)}</td>`;mt.appendChild(tr);}
}
$('#openTaskForm').onclick=()=>{$('#taskFormBox').style.display='block';};
$('#taskForm').addEventListener('submit',async e=>{e.preventDefault();if(!currentRun)return;
  const f=new FormData(e.target);const body={};
  for(const[k,v]of f.entries())if(v)body[k]=v;
  if(body.write_scope)body.write_scope=body.write_scope.split(',').map(s=>s.trim());
  const r=await api(`/api/runs/${currentRun}/tasks`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(r.status!==200)alert(r.body.error||'create failed');e.target.reset();loadDetail();});
$('#btnPause').onclick=()=>controlRun('pause');$('#btnResume').onclick=()=>controlRun('resume');
async function controlRun(action){if(!currentRun)return;
  const r=await api(`/api/runs/${currentRun}/${action}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reason:'console-'+action})});
  if(r.status!==200)alert(r.body.error||action+' failed');else loadDetail();}

// ---- Teams ----
const backendOptions=['fake','codex','codebuddy'];const roleOptions=['worker','supervisor'];
function poolRowHTML(p){p=p||{};
 return `<div class="pool-row" data-row>
  <label>backend<select class="p-backend">${backendOptions.map(b=>'<option'+(b===p.backend?' selected':'')+'>'+b+'</option>').join('')}</select></label>
  <label>role<select class="p-role">${roleOptions.map(r=>'<option'+(r===(p.role_id||'worker')?' selected':'')+'>'+r+'</option>').join('')}</select></label>
  <label>pool_id<input class="p-id" value="${esc(p.pool_id||'')}" placeholder="pool-1"></label>
  <label>count<input class="p-count" type="number" min="1" value="${p.count||1}"></label>
  <label>max<input class="p-max" type="number" min="1" value="${p.max_count||p.count||1}"></label>
  <button class="ghost" data-del>删除</button></div>`;}
let poolRows=[{backend:'fake',role_id:'worker',pool_id:'pool-fake',count:2,max_count:2}];
function renderPoolRows(){const box=$('#poolRows');box.innerHTML='';
  poolRows.forEach((p,i)=>{const div=document.createElement('div');div.innerHTML=poolRowHTML(p);
    div.querySelector('[data-del]').onclick=()=>{poolRows.splice(i,1);renderPoolRows();};
    box.appendChild(div);});renderTeamPreview();}
$('#addPool').onclick=()=>{poolRows.push({backend:'fake',role_id:'worker',count:1,max_count:1});renderPoolRows();};
function collectTeam(){
  const pools=[...$('#poolRows').querySelectorAll('[data-row]')].map(row=>({
    pool_id: row.querySelector('.p-id').value||('pool-'+Math.random().toString(16).slice(2,6)),
    backend: row.querySelector('.p-backend').value, role_id: row.querySelector('.p-role').value,
    count: parseInt(row.querySelector('.p-count').value)||1, max_count: parseInt(row.querySelector('.p-max').value)||1 }));
  return {schema_version:1,team_id:$('#newTeamId').value||'ad-hoc-team',bootstrap_supervisor:'supervisor',
    roles:[{role_id:'worker',version:1,title:'Implementation Worker',required_capabilities:['read','write','test']},
           {role_id:'supervisor',version:1,title:'Supervisor and Reviewer',required_capabilities:['plan','review']}],
    agent_pools:pools};
}
function renderTeamPreview(){$('#teamPreview').textContent=JSON.stringify(collectTeam(),null,2);}
$('#newTeamId').addEventListener('input',renderTeamPreview);
$('#saveTeam').onclick=async()=>{
  const r=await api('/api/teams',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(collectTeam())});
  alert(r.status===200?(r.body.path||'saved'):(r.body.error||'save failed'));loadTeams();};
$('#saveAsDefault').onclick=async()=>{
  const team=collectTeam();
  const r=await api('/api/teams',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({team,as_default:true})});
  alert(r.status===200?(r.body.path||'saved'):(r.body.error||'save failed'));loadTeams();};
async function loadTeams(){
  renderPoolRows();
  const t=await api('/api/teams');
  const tb=$('#teamsTbl tbody');tb.innerHTML='';
  for(const team of t.body.teams||[]){
    const detail=team.team?team.team.agent_pools.map(p=>p.backend+'x'+p.count).join(', '):'(默认)';
    const tr=document.createElement('tr');
    tr.innerHTML=`<td class="mono">${esc(team.team_id)}</td><td>${esc(team.source)}</td><td>${esc(detail)}</td>
      <td><button class="ghost" data-load="${esc(team.path||'')}" data-team='${esc(JSON.stringify(team.team||{}))}'>载入编辑器</button></td>`;
    tb.appendChild(tr);}
  tb.querySelectorAll('[data-load]').forEach(b=>b.onclick=()=>{
    if(b.dataset.team&&b.dataset.team!==''&&JSON.stringify(poolRows)!==''){const data=JSON.parse(b.dataset.team);
      $('#newTeamId').value=data.team_id;
      poolRows=(data.agent_pools||[]).map(p=>({backend:p.backend,role_id:p.role_id,pool_id:p.pool_id,count:p.count,max_count:p.max_count}));
      renderPoolRows();}});}

// ---- Connections ----
async function loadConnections(){
  const c=await api('/api/connections');const tb=$('#connTbl tbody');tb.innerHTML='';
  for(const x of c.body.connections||[]){const tr=document.createElement('tr');
    tr.innerHTML=`<td class="mono">${esc(x.backend)}</td>
      <td>${x.cli_available?'<span class="badge badge-ok">可用</span>':'<span class="badge badge-warn">未找到 CLI</span>'}</td>
      <td>${esc(x.version||'-')}</td><td class="mono">${esc(x.login_command||'无需登录')}</td><td class="muted">${esc(x.note||'')}</td>`;
    tb.appendChild(tr);}
}
$('#probeBtn').onclick=loadConnections;

// ---- boot ----
(async function(){await Promise.all([loadRuns(),loadConnections()]);loadTeams();
  const st=await api('/api/status');
  $('#conn').textContent=`project: ${st.body.project_root}`;
  const init=st.body.run_id;if(init){currentRun=init;$('#detailTitle').textContent='· '+init;loadDetail();}})();
setInterval(()=>{if(document.querySelector('nav button.active').dataset.tab==='runs')loadRuns();},8000);
</script></body></html>
"""
