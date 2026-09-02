"""T3：本地状态页 + 管理控制台（HTTP 服务）。

只读 API 查询 SQLite 状态；写操作（发起任务、取消、暂停/恢复）复用 store 的
controller/authority fencing——控制台启动时尝试 acquire Run controller 与
authority；成功则以"人工操作者"身份提供写操作，失败则退化为只读状态页
（例如 serve 常驻循环正在持有协调权时）。

用法：
    & '.venv\\Scripts\\python.exe' -m orchestrator console --db ... --run run-1 --port 8080
"""

from __future__ import annotations

import argparse
import json
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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
        # 不把 SQL 参数/路径打日志，避免泄露敏感信息
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
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    # -- 路由 ---------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/" or path == "/index.html":
            self._serve_index()
            return
        if path == "/api/status":
            self._get_status()
        elif path.startswith("/api/runs/"):
            self._get_run_resource(path)
        else:
            self._send_error_json(404, "not found")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        body = self._read_json_body()
        if path.startswith("/api/runs/"):
            self._post_run_action(path, body)
        else:
            self._send_error_json(404, "not found")

    # -- 静态页 -------------------------------------------------------------

    def _serve_index(self) -> None:
        data = _DEFAULT_INDEX_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # -- 只读 API -----------------------------------------------------------

    def _get_status(self) -> None:
        store = self.server.store
        try:
            status = {
                "writable": self.server.writable,
                "owner": self.server.owner,
                "run_id": self.server.run_id,
                "controller_epoch": (
                    self.server.controller.epoch
                    if self.server.controller is not None
                    else None
                ),
                "summary": store.summary(run_id=self.server.run_id),
            }
        except Exception as exc:  # noqa: BLE001
            self._send_error_json(500, type(exc).__name__)
            return
        self._send_json(status)

    def _get_run_resource(self, path: str) -> None:
        parts = path.strip("/").split("/")
        # /api/runs/{run_id}/{resource}
        if len(parts) != 4 or parts[0] != "api" or parts[1] != "runs":
            self._send_error_json(404, "not found")
            return
        run_id, resource = parts[2], parts[3]
        store = self.server.store
        try:
            if resource == "tasks":
                payload = self._tasks(run_id)
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

    def _tasks(self, run_id: str) -> list[dict[str, Any]]:
        return _rows(
            self.server.store.connection.execute(
                "SELECT * FROM tasks WHERE run_id=? ORDER BY created_at, task_id",
                (run_id,),
            ).fetchall()
        )

    # -- 写操作（管理控制台） ------------------------------------------------

    def _post_run_action(self, path: str, body: dict[str, Any]) -> None:
        parts = path.strip("/").split("/")
        # /api/runs/{run_id}/tasks
        # /api/runs/{run_id}/tasks/{task_id}/cancel
        # /api/runs/{run_id}/pause | resume
        if len(parts) < 3 or parts[0] != "api" or parts[1] != "runs":
            self._send_error_json(404, "not found")
            return
        run_id = parts[2]
        action = parts[3] if len(parts) > 3 else ""
        if not self.server.writable:
            self._send_error_json(
                409,
                "console is read-only (Run controller is held by another owner)",
            )
            return
        try:
            if action == "tasks" and len(parts) >= 6 and parts[5] == "cancel":
                self._cancel_task(run_id, parts[4], body)
                return
            if action == "tasks":
                self._create_task(run_id, body)
                return
            if action == "pause":
                self.server.store.pause_run(
                    run_id,
                    self.server.controller,
                    reason=str(body.get("reason") or "console-pause"),
                )
                self._send_json({"ok": True})
                return
            if action == "resume":
                self.server.store.resume_run(
                    run_id,
                    self.server.controller,
                    reason=str(body.get("reason") or "console-resume"),
                )
                self._send_json({"ok": True})
                return
        except (FencedControllerError, FencedAuthorityError) as exc:
            self._send_error_json(409, str(exc))
            return
        except ValueError as exc:
            self._send_error_json(400, str(exc))
            return
        except KeyError as exc:
            self._send_error_json(404, str(exc))
            return
        self._send_error_json(404, "not found")

    def _create_task(self, run_id: str, body: dict[str, Any]) -> None:
        task_id = str(body.get("task_id") or f"task-{uuid.uuid4().hex[:12]}")
        access_mode = str(body.get("access_mode") or "read_only")
        write_scope = tuple(body.get("write_scope") or ())
        prompt = str(body.get("prompt") or task_id)
        cwd = str(body.get("cwd") or self.server.worktree or ".")
        timeout = float(body.get("timeout_seconds") or 60)
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
        self._send_json({"ok": True, "task_id": task_id})

    def _cancel_task(self, run_id: str, task_id: str, body: dict[str, Any]) -> None:
        self.server.store.request_cancel_task(
            task_id,
            self.server.controller,
            reason=str(body.get("reason") or "console-cancel"),
        )
        self._send_json({"ok": True, "task_id": task_id})


class ConsoleHTTPServer(HTTPServer):
    # 本地小流量工具：单线程串行处理，避免 sqlite 连接跨线程
    def __init__(
        self,
        store: SQLiteStateStore,
        run_id: str,
        *,
        host: str = "127.0.0.1",
        port: int = 8080,
        owner: str = "human-ops",
        worktree: str | None = None,
    ) -> None:
        self.store = store
        self.run_id = run_id
        self.owner = owner
        self.worktree = worktree
        self.controller = store.acquire_run_controller(
            run_id, owner, lease_seconds=3600
        )
        try:
            self.authority = store.acquire_authority(
                run_id, owner, "supervisor", lease_seconds=3600
            )
        except FencedAuthorityError:
            self.authority = None
        self.writable = self.controller is not None and self.authority is not None
        if self.controller is not None and self.authority is None:
            # 只读：释放 controller，避免长期占着协调权却不干活
            store.release_run_controller(self.controller)
            self.controller = None
        super().__init__((host, port), ConsoleHandler)

    def close(self) -> None:
        try:
            if self.controller is not None:
                self.store.release_run_controller(self.controller)
        except FencedControllerError:
            pass
        self.server_close()


def run_console(
    *,
    db: Path,
    run_id: str,
    port: int = 8080,
    host: str = "127.0.0.1",
    owner: str = "human-ops",
    worktree: str | None = None,
) -> None:
    store = SQLiteStateStore(db)
    server = ConsoleHTTPServer(
        store,
        run_id,
        host=host,
        port=port,
        owner=owner,
        worktree=worktree,
    )
    mode = "read-write" if server.writable else "read-only"
    print(f"console listening on http://{host}:{port}  (mode: {mode}, run: {run_id})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="T3 local status page + console")
    parser.add_argument("--db", type=Path, default=Path(".agent-hub/state/agent-hub.db"))
    parser.add_argument("--run", dest="run_id", required=True)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--owner", default="human-ops")
    parser.add_argument("--worktree", default=None)
    args = parser.parse_args(argv)
    run_console(
        db=args.db,
        run_id=args.run_id,
        port=args.port,
        host=args.host,
        owner=args.owner,
        worktree=args.worktree,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


_DEFAULT_INDEX_HTML = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>Agent Hub Console</title>
<style>
body{font-family:ui-sans-serif,system-ui,sans-serif;margin:0;background:#0f172a;color:#e2e8f0}
header{padding:16px 24px;background:#1e293b;display:flex;justify-content:space-between;align-items:center}
h1{font-size:18px;margin:0}
main{max-width:1200px;margin:0 auto;padding:24px}
section{margin-bottom:24px}
h2{font-size:14px;color:#94a3b8;text-transform:uppercase;letter-spacing:.05em}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid #334155}
th{color:#94a3b8;font-weight:600}
.badge{display:inline-block;padding:2px 8px;border-radius:9999px;font-size:11px}
.badge-completed{background:#166534;color:#86efac}.badge-review{background:#7c2d12;color:#fdba74}
.badge-failed{background:#7f1d1d;color:#fca5a5}.badge-pending{background:#1e3a8a;color:#93c5fd}
button{background:#2563eb;color:#fff;border:0;padding:6px 12px;border-radius:6px;cursor:pointer}
form{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px}
input,select{padding:6px 8px;border-radius:6px;border:1px solid #334155;background:#1e293b;color:#e2e8f0}
.muted{color:#64748b}
</style></head><body>
<header><h1>Agent Hub Console</h1><span id="status" class="muted">loading…</span></header>
<main>
<section><h2>发起任务</h2>
<form id="taskForm">
<input name="task_id" placeholder="task_id（留空自动生成）">
<select name="access_mode"><option>read_only</option><option>write</option></select>
<input name="write_scope" placeholder="write_scope（如 demo/a.txt）">
<input name="prompt" placeholder="prompt" size="40">
<input name="cwd" placeholder="cwd">
<button type="submit">发起</button></form></section>
<section><h2>任务</h2><table id="tasks"><thead><tr>
<th>task</th><th>状态</th><th>模式</th><th>scope</th><th>attempts</th><th></th></tr></thead>
<tbody></tbody></table></section>
<section><h2>审批</h2><table id="approvals"><thead><tr>
<th>id</th><th>task</th><th>发起人</th><th>原因</th><th>状态</th></tr></thead>
<tbody></tbody></table></section>
<section><h2>Merge Queue</h2><table id="merges"><thead><tr>
<th>merge</th><th>task</th><th>result commit</th><th>状态</th></tr></thead>
<tbody></tbody></table></section>
</main>
<script>
let runId = '';
async function api(path, options){const r=await fetch(path, options);return r.json();}
function badge(state){const s=(state||'').toLowerCase();
const cls=s.includes('complete')?'badge-completed':s.includes('review')?'badge-review':
s.includes('fail')||s.includes('cancel')?'badge-failed':'badge-pending';
return `<span class="badge ${cls}">${state||''}</span>`;}
async function loadStatus(){const st=await api('/api/status');runId=st.run_id;
document.getElementById('status').textContent=`${st.writable?'读写':'只读'} · run=${st.run_id} · owner=${st.owner}`;}
async function loadTasks(){const d=await api(`/api/runs/${runId}/tasks`);
const tb=document.querySelector('#tasks tbody');tb.innerHTML='';
for(const t of d.tasks||[]){const tr=document.createElement('tr');
tr.innerHTML=`<td>${t.task_id}</td><td>${badge(t.state)}</td><td>${t.access_mode}</td>
<td>${(t.write_scope_json||'').slice(0,60)}</td>
<td>${t.attempt_count ?? ''}</td>
<td><button onclick="cancelTask('${t.task_id}')">取消</button></td>`;
tb.appendChild(tr);}}
async function cancelTask(taskId){await api(`/api/runs/${runId}/tasks/${taskId}/cancel`,
{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reason:'console-cancel'})});
loadTasks();}
async function loadApprovals(){const d=await api(`/api/runs/${runId}/approvals`);
const tb=document.querySelector('#approvals tbody');tb.innerHTML='';
for(const a of d.approvals||[]){const tr=document.createElement('tr');
tr.innerHTML=`<td>${a.approval_id}</td><td>${a.task_id||''}</td><td>${a.requested_by||''}</td>
<td>${a.reason||''}</td><td>${badge(a.status)}</td>`;tb.appendChild(tr);}}
async function loadMerges(){const d=await api(`/api/runs/${runId}/merges`);
const tb=document.querySelector('#merges tbody');tb.innerHTML='';
for(const m of d.merges||[]){const tr=document.createElement('tr');
tr.innerHTML=`<td>${m.merge_id}</td><td>${m.task_id}</td><td>${(m.result_commit||'').slice(0,10)}</td>
<td>${badge(m.status)}</td>`;tb.appendChild(tr);}}
document.getElementById('taskForm').addEventListener('submit',async e=>{e.preventDefault();
const f=new FormData(e.target);const body={};
for(const [k,v] of f.entries()) if(v) body[k]=v;
if(body.write_scope) body.write_scope=body.write_scope.split(',').map(s=>s.trim());
await api(`/api/runs/${runId}/tasks`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
e.target.reset();loadTasks();});
loadStatus();loadTasks();loadApprovals();loadMerges();
setInterval(loadStatus,5000);setInterval(loadTasks,5000);
</script></body></html>
"""
