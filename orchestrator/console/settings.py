"""网页控制台：本地设置、Team 配置持久化与连接探测。

- 设置与保存的 team 只放 ``.agent-hub/``（状态目录）。
- 不存储任何 token / cookie / credential：Codex 用 saved login、CodeBuddy 用
  CLI 登录态，这里只做探测与登录引导。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

DEFAULT_SETTINGS: dict[str, Any] = {
    "default_team": "config/team.yaml",
    "default_backend": "fake",
    "model_providers": [],
    "projects": [],
}

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_FIELDS = {
    "api_key",
    "apiKey",
    "auth_token",
    "authToken",
    "token",
    "secret",
    "secret_key",
    "secretKey",
}


def _normalize_model_provider(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("model provider must be an object")
    forbidden = sorted(_SECRET_FIELDS.intersection(raw))
    if forbidden:
        raise ValueError("model provider must reference a key environment variable, not store a secret")
    provider_id = str(raw.get("provider_id") or "").strip()
    if not provider_id or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", provider_id):
        raise ValueError("provider_id must contain only letters, numbers, '.', '_' or '-'")
    backend = str(raw.get("backend") or "codebuddy").strip()
    if backend not in {"codebuddy"}:
        raise ValueError("custom model providers currently use the codebuddy backend")
    label = str(raw.get("label") or provider_id).strip()
    if not label:
        raise ValueError("provider label must not be empty")
    base_url = str(raw.get("base_url") or "").strip()
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base_url must be an http(s) URL")
    if parsed.username or parsed.password:
        raise ValueError("base_url must not contain credentials")
    api_key_env = str(raw.get("api_key_env") or "").strip()
    if not _ENV_NAME.fullmatch(api_key_env):
        raise ValueError("api_key_env must be a valid environment variable name")
    models_raw = raw.get("models")
    if not isinstance(models_raw, list) or not models_raw:
        raise ValueError("models must be a non-empty list")
    models: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in models_raw:
        if isinstance(item, str):
            model_id, model_label = item.strip(), item.strip()
        elif isinstance(item, dict):
            model_id = str(item.get("id") or item.get("model") or "").strip()
            model_label = str(item.get("label") or item.get("name") or model_id).strip()
        else:
            raise ValueError("each provider model must be a string or object")
        if not model_id or model_id in seen:
            if not model_id:
                raise ValueError("provider model id must not be empty")
            continue
        seen.add(model_id)
        models.append({"id": model_id, "label": model_label or model_id})
    default_model = str(raw.get("default_model") or "").strip() or None
    if default_model and default_model not in seen:
        raise ValueError("default_model must reference one of models")
    result: dict[str, Any] = {
        "provider_id": provider_id,
        "label": label,
        "backend": backend,
        "base_url": base_url,
        "api_key_env": api_key_env,
        "models": models,
    }
    if default_model:
        result["default_model"] = default_model
    return result


def load_model_providers(project_root: Path) -> list[dict[str, Any]]:
    raw = load_settings(project_root).get("model_providers", [])
    if not isinstance(raw, list):
        return []
    providers: list[dict[str, Any]] = []
    for item in raw:
        try:
            providers.append(_normalize_model_provider(item))
        except ValueError:
            continue
    return providers


def save_model_provider(project_root: Path, provider: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_model_provider(provider)
    settings = load_settings(project_root)
    providers = load_model_providers(project_root)
    providers = [
        item
        for item in providers
        if item["provider_id"] != normalized["provider_id"]
    ]
    providers.append(normalized)
    settings["model_providers"] = providers
    save_settings(project_root, settings)
    return normalized


def delete_model_provider(project_root: Path, provider_id: str) -> bool:
    settings = load_settings(project_root)
    providers = load_model_providers(project_root)
    remaining = [item for item in providers if item["provider_id"] != provider_id]
    if len(remaining) == len(providers):
        return False
    settings["model_providers"] = remaining
    save_settings(project_root, settings)
    return True


def hub_dir(project_root: Path) -> Path:
    return project_root / ".agent-hub"


def settings_path(project_root: Path) -> Path:
    return hub_dir(project_root) / "settings.json"


def load_settings(project_root: Path) -> dict[str, Any]:
    path = settings_path(project_root)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                merged = dict(DEFAULT_SETTINGS)
                merged.update(data)
                return merged
        except (json.JSONDecodeError, OSError):
            pass
    return dict(DEFAULT_SETTINGS)


def save_settings(project_root: Path, settings: dict[str, Any]) -> Path:
    path = settings_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def _normalize_project(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("project must be an object")
    project_id = str(raw.get("project_id") or "").strip()
    if not project_id or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", project_id):
        raise ValueError("project_id must contain only letters, numbers, '.', '_' or '-'")
    name = str(raw.get("name") or project_id).strip()
    workspace_raw = str(raw.get("workspace") or "").strip()
    if not workspace_raw:
        raise ValueError("workspace must not be empty")
    workspace = Path(workspace_raw).expanduser()
    if not workspace.is_absolute():
        raise ValueError("workspace must be an absolute path")
    try:
        resolved = workspace.resolve(strict=True)
    except OSError as exc:
        raise ValueError("workspace directory does not exist") from exc
    if not resolved.is_dir():
        raise ValueError("workspace must be a directory")
    default_team = str(raw.get("default_team") or "config/team.yaml").strip()
    if not default_team:
        default_team = "config/team.yaml"
    return {
        "project_id": project_id,
        "name": name or project_id,
        "workspace": str(resolved),
        "default_team": default_team,
    }


def list_projects(project_root: Path) -> list[dict[str, Any]]:
    """Return the current workspace plus saved local project entries."""
    current = {
        "project_id": "current",
        "name": project_root.name or str(project_root),
        "workspace": str(project_root.resolve()),
        "default_team": load_settings(project_root).get("default_team") or "config/team.yaml",
        "current": True,
    }
    projects = [current]
    seen = {current["workspace"]}
    raw = load_settings(project_root).get("projects", [])
    if not isinstance(raw, list):
        return projects
    for item in raw:
        try:
            normalized = _normalize_project(item)
        except ValueError:
            continue
        if normalized["workspace"] in seen:
            continue
        normalized["current"] = False
        seen.add(normalized["workspace"])
        projects.append(normalized)
    return projects


def save_project(project_root: Path, project: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_project(project)
    settings = load_settings(project_root)
    existing = settings.get("projects", [])
    if not isinstance(existing, list):
        existing = []
    remaining = []
    for item in existing:
        try:
            item_norm = _normalize_project(item)
        except ValueError:
            continue
        if item_norm["project_id"] != normalized["project_id"]:
            remaining.append(item_norm)
    remaining.append(normalized)
    settings["projects"] = remaining
    save_settings(project_root, settings)
    return normalized


def delete_project(project_root: Path, project_id: str) -> bool:
    settings = load_settings(project_root)
    existing = settings.get("projects", [])
    if not isinstance(existing, list):
        return False
    remaining = [
        item for item in existing
        if not isinstance(item, dict) or str(item.get("project_id") or "") != project_id
    ]
    if len(remaining) == len(existing):
        return False
    settings["projects"] = remaining
    save_settings(project_root, settings)
    return True


def teams_dir(project_root: Path) -> Path:
    return hub_dir(project_root) / "teams"


def default_team_path(project_root: Path) -> Path:
    """默认团队：项目仓库 config/team.yaml（若存在）。"""
    candidate = project_root / "config" / "team.yaml"
    return candidate if candidate.is_file() else candidate


def save_team_config(project_root: Path, team: dict[str, Any]) -> Path:
    """把组建的 team 配置保存到 .agent-hub/teams/<team_id>.json（可 load_team_spec）。"""
    team_id = str(team.get("team_id") or "custom-team")
    directory = teams_dir(project_root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{team_id}.json"
    path.write_text(
        json.dumps(team, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def list_saved_teams(project_root: Path) -> list[dict[str, Any]]:
    """列出默认 team + 已保存 team，返回可直接展示的 dict。"""
    teams: list[dict[str, Any]] = []
    default = default_team_path(project_root)
    if default.is_file():
        # The UI historically called the project config ``default``.  That
        # alias is useful for old databases, but new Runs must use the actual
        # team_id from the file so the scheduler can bind its agents.
        default_team_id = "default"
        try:
            data = json.loads(default.read_text(encoding="utf-8"))
            if isinstance(data, dict) and str(data.get("team_id") or "").strip():
                default_team_id = str(data["team_id"])
        except (json.JSONDecodeError, OSError):
            pass
        teams.append(
            {
                "team_id": default_team_id,
                "source": "default",
                "path": str(default),
            }
        )
    directory = teams_dir(project_root)
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if isinstance(data, dict):
                teams.append(
                    {
                        "team_id": str(data.get("team_id") or path.stem),
                        "source": "saved",
                        "path": str(path),
                        "team": data,
                    }
                )
    return teams


def _codebuddy_auth_dir() -> Path | None:
    """CodeBuddy CLI 认证目录（仅判断路径，绝不读取凭证内容）。"""
    import os

    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    return (
        Path(local)
        / "CodeBuddyExtension"
        / "Data"
        / "Public"
        / "auth"
    )


def _codebuddy_auth_visibility() -> str:
    """Return whether the current process can inspect CodeBuddy's auth store.

    A missing directory is different from an unreadable directory.  In
    particular, the Codex desktop sandbox can start the console with a
    restricted token: the user's CodeBuddy auth directory exists, but a
    child process cannot enumerate it.  Treating that case as ``False``
    would incorrectly tell the user to log in again.
    """
    auth_dir = _codebuddy_auth_dir()
    if auth_dir is None or not auth_dir.exists():
        return "missing"
    try:
        list(auth_dir.iterdir())
    except OSError:
        return "unreadable"
    return "readable"


def codebuddy_login_state() -> bool | None:
    """探测 CodeBuddy CLI 登录态（启发式：只检查标记文件存在性）。

    返回 True=已登录 / False=未登录 / None=无法判定（目录不存在或读取被拒）。
    不读取、不打印任何凭证内容。
    """
    auth_dir = _codebuddy_auth_dir()
    if auth_dir is None or not auth_dir.is_dir():
        return None
    try:
        entries = list(auth_dir.iterdir())
    except OSError:
        return None
    logged_out = any(
        p.name.endswith(".logged-out") or ".logged-out" in p.name for p in entries
    )
    info_files = []
    for path in entries:
        if not path.name.endswith(".info") or ".logged-out" in path.name:
            continue
        try:
            if path.stat().st_size > 0:
                info_files.append(path)
        except OSError:
            return None
    if logged_out:
        return False
    if info_files:
        return True
    return None


def _codebuddy_sdk_login_state(codebuddy_path: str) -> bool | None:
    """Use CodeBuddy's local initialize handshake when markers are inconclusive.

    The SDK returns an already-resolved flow for an existing login.  When no
    login exists it returns a flow carrying a one-time URL; cancel that flow
    immediately so probing never opens a browser or waits for user input.
    No account, token, or URL is returned from this helper.
    """
    try:
        import asyncio

        from codebuddy_agent_sdk import authenticate
        from orchestrator.adapters.codebuddy_config import (
            CODEBUDDY_REGION,
            codebuddy_china_environment,
        )

        async def probe() -> bool:
            flow = await authenticate(
                environment=CODEBUDDY_REGION,
                env=codebuddy_china_environment(),
                codebuddy_code_path=codebuddy_path,
                timeout=8.0,
            )
            if flow.auth_url:
                await flow.cancel()
                return False
            return True

        return asyncio.run(probe())
    except Exception:  # noqa: BLE001 - probe must remain non-blocking/read-only
        return None


def _login_window_script(project_root: Path, backend: str) -> Path:
    """生成后端登录引导脚本（.agent-hub/login/<backend>-login.cmd）。

    脚本本身只做四件事：切到项目目录、设好环境变量、启动 CLI 登录流程、暂停等待用户查看。
    CLI 绝对路径在写文件时加引号，规避 cmd 嵌套引号转义坑。
    """
    script_dir = hub_dir(project_root) / "login"
    script_dir.mkdir(parents=True, exist_ok=True)
    path = script_dir / f"{backend}-login.cmd"

    if backend == "codex":
        cli = shutil.which("codex") or _local_cli_path(project_root, "codex")
        if cli is None:
            raise FileNotFoundError("codex CLI not found")
        body = (
            "@echo off\r\n"
            "setlocal\r\n"
            f'cd /d "{project_root}"\r\n'
            "echo ================================================\r\n"
            "echo   Codex login helper\r\n"
            "echo   A browser window will open - authorize there,\r\n"
            "echo   then close this window and refresh the console.\r\n"
            "echo ================================================\r\n"
            f'"{cli}" login\r\n'
            "echo.\r\n"
            "pause\r\n"
        )
    elif backend == "codebuddy":
        import os

        cli = (
            os.environ.get("AGENT_HUB_CODEBUDDY_BIN")
            or os.environ.get("CODEBUDDY_CODE_PATH")
            or None
        )
        if not cli or not Path(cli).is_file():
            local = _local_cli_path(project_root, "codebuddy", "codebuddy-code")
            cli = str(local) if local else shutil.which("codebuddy")
        if not cli:
            raise FileNotFoundError("codebuddy CLI not found")
        body = (
            "@echo off\r\n"
            "setlocal\r\n"
            f'cd /d "{project_root}"\r\n'
            "set CODEBUDDY_SKIP_GIT_BASH_CHECK=1\r\n"
            "set CODEBUDDY_INTERNET_ENVIRONMENT=internal\r\n"
            "echo ============================================================\r\n"
            "echo   CodeBuddy browser login helper\r\n"
            "echo   A browser will open for authorization automatically.\r\n"
            "echo   Complete login in the browser, then return to the console.\r\n"
            "echo ============================================================\r\n"
            f'"{sys.executable}" -m orchestrator auth codebuddy --open-browser\r\n'
            "echo.\r\n"
            "pause\r\n"
        )
    else:
        raise ValueError(f"backend has no login flow: {backend}")

    path.write_text(body, encoding="ascii")
    return path


def launch_login(
    project_root: Path,
    backend: str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """启动后端登录引导（CodeBuddy 使用后台浏览器认证）。

    ``dry_run=True`` 只生成回退脚本，不启动桌面进程，供测试使用。

    返回 dict：ok / message / script（生成的引导脚本路径）。
    """
    try:
        script = _login_window_script(project_root, backend)
    except (FileNotFoundError, ValueError) as exc:
        return {"ok": False, "message": str(exc), "script": None}
    if dry_run:
        return {"ok": True, "message": "script ready", "script": str(script)}
    if backend == "codebuddy":
        # CodeBuddy's SDK authentication flow can open the one-time browser URL
        # directly and wait for the callback.  Running it detached avoids the
        # fragile visible-CLI + synthetic ``/login`` keystroke flow.
        import os
        import sys
        import time

        launch_env = os.environ.copy()
        launch_env["CODEBUDDY_SKIP_GIT_BASH_CHECK"] = "1"
        launch_env["CODEBUDDY_INTERNET_ENVIRONMENT"] = "internal"
        source_root = Path(__file__).resolve().parents[2]
        pythonpath = [str(source_root)]
        existing_pythonpath = launch_env.get("PYTHONPATH")
        if existing_pythonpath:
            pythonpath.append(existing_pythonpath)
        launch_env["PYTHONPATH"] = os.pathsep.join(pythonpath)
        venv_python = project_root / ".venv" / Path(
            "Scripts/python.exe" if os.name == "nt" else "bin/python"
        )
        python_executable = str(venv_python) if venv_python.is_file() else sys.executable
        command = [
            python_executable,
            "-m",
            "orchestrator",
            "auth",
            "codebuddy",
            "--open-browser",
        ]
        try:
            creationflags = 0
            if sys.platform == "win32":
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                    subprocess, "DETACHED_PROCESS", 0
                )
            process = subprocess.Popen(
                command,
                cwd=str(project_root),
                env=launch_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
                start_new_session=sys.platform != "win32",
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {
                "ok": False,
                "message": f"cannot start background browser login: {exc}",
                "script": str(script),
            }
        time.sleep(0.25)
        returncode = process.poll()
        if returncode is not None:
            return {
                "ok": False,
                "message": (
                    "CodeBuddy authorization process exited before the browser flow "
                    f"started (exit code {returncode})"
                ),
                "script": str(script),
                "mode": "background-browser",
            }
        return {
            "ok": True,
            "message": "CodeBuddy browser authorization started",
            "script": str(script),
            "mode": "background-browser",
            "pid": process.pid,
        }
    try:
        import os

        if sys.platform == "win32":
            # 通过已登录桌面会话的 ShellExecute 执行，等同于用户双击脚本。
            # 直接 Popen(cmd.exe) 会继承控制台宿主的限制，CodeBuddy 可能因此
            # 无法写入用户级认证目录；explorer.exe <file> 又只会定位文件。
            launch_env = os.environ.copy()
            launch_env["AGENT_HUB_LOGIN_SCRIPT"] = str(script)
            launch_env["AGENT_HUB_LOGIN_CWD"] = str(project_root)
            shell_execute = (
                "$shell = New-Object -ComObject Shell.Application; "
                "$shell.ShellExecute($env:AGENT_HUB_LOGIN_SCRIPT, '', "
                "$env:AGENT_HUB_LOGIN_CWD, 'open', 1)"
            )
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-WindowStyle",
                    "Hidden",
                    "-Command",
                    shell_execute,
                ],
                env=launch_env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode != 0:
                message = (result.stderr or result.stdout or "ShellExecute failed").strip()
                raise OSError(message)
        else:
            subprocess.Popen(
                ["cmd", "/k", str(script)],
                start_new_session=True,
            )
    except (OSError, subprocess.TimeoutExpired) as exc:  # 无桌面会话或 Shell 不可用
        return {
            "ok": False,
            "message": f"cannot open a terminal window here: {exc}",
            "script": str(script),
        }
    return {
        "ok": True,
        "message": "login helper window opened",
        "script": str(script),
    }


def _local_cli_path(project_root: Path, *names: str) -> Path | None:
    """项目内受管 CLI：.agent-hub/tools/node_modules/.bin/<name>。

    Windows 优先 .cmd（无扩展名的是 bash shim，subprocess 无法直接执行）；
    非 Windows 优先无扩展名可执行文件。
    """
    import sys

    bin_dir = project_root / ".agent-hub" / "tools" / "node_modules" / ".bin"
    win = sys.platform == "win32"
    for name in names:
        candidates = (
            [bin_dir / f"{name}.cmd", bin_dir / f"{name}.ps1", bin_dir / name]
            if win
            else [bin_dir / name, bin_dir / f"{name}.cmd"]
        )
        for candidate in candidates:
            if candidate.is_file():
                return candidate
    return None


def _run_version(cli: str | Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            [str(cli), *args],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        out = (result.stdout or "") + (result.stderr or "")
        for line in out.splitlines():
            line = line.strip()
            if line and not line.startswith("-"):
                return line[:80]
        return None
    except (OSError, subprocess.SubprocessError):
        return None


def probe_connections(project_root: Path | None = None) -> list[dict[str, Any]]:
    """探测各后端 CLI 可达性（只探测版本/存在性，绝不读取或打印凭证）。

    Codex：PATH 上的 codex，或项目 .agent-hub/tools 内。
    CodeBuddy：项目 .agent-hub/tools/node_modules/.bin/codebuddy（受管安装），
              也兼容 PATH 上的 codebuddy；用 AGENT_HUB_CODEBUDDY_BIN 覆盖。
    登录态需要用户在终端完成（CodeBuddy 认证目录不被网页触碰）。
    """
    root = project_root or Path.cwd()
    import os

    # Codex
    codex_cli = shutil.which("codex")
    codex_local = _local_cli_path(root, "codex")
    codex_path = codex_cli or (str(codex_local) if codex_local else None)
    codex_version = _run_version(codex_path, "--version") if codex_path else None
    codex_login = (
        _run_version(codex_path, "login", "status") if codex_path else None
    )
    codex_logged_in = bool(codex_login and "logged in" in codex_login.lower())

    # CodeBuddy：显式环境变量 > 项目受管安装 > PATH
    codebuddy_path: str | None = (
        os.environ.get("AGENT_HUB_CODEBUDDY_BIN")
        or os.environ.get("CODEBUDDY_CODE_PATH")
        or None
    )
    if not codebuddy_path or not Path(codebuddy_path).is_file():
        cb_local = _local_cli_path(root, "codebuddy", "codebuddy-code")
        if cb_local:
            codebuddy_path = str(cb_local)
        else:
            codebuddy_path = shutil.which("codebuddy")
    codebuddy_version = (
        _run_version(codebuddy_path, "--version") if codebuddy_path else None
    )
    auth_visibility = _codebuddy_auth_visibility() if codebuddy_path else "missing"
    codebuddy_logged_in = codebuddy_login_state() if codebuddy_path else None
    if codebuddy_logged_in is None and codebuddy_path and auth_visibility != "unreadable":
        # A readable directory without a decisive marker can still be checked
        # through the SDK initialize handshake.  Never run this fallback when
        # the current process cannot inspect the auth directory: a restricted
        # child would otherwise turn a permission failure into "logged out".
        auth_dir = _codebuddy_auth_dir()
        if auth_dir is not None and auth_dir.exists():
            codebuddy_logged_in = _codebuddy_sdk_login_state(codebuddy_path)

    return [
        {
            "backend": "codex",
            "label": "OpenAI Codex",
            "cli_available": codex_version is not None,
            "version": codex_version,
            "cli_path": codex_path,
            "logged_in": codex_logged_in,
            "login_capable": codex_path is not None,
            "login_command": "codex login",
            "login_status_hint": (
                f"检测到已登录：{codex_login}" if codex_logged_in
                else "终端执行 `codex login status` 查看；未登录请运行 `codex login`。"
            ),
            "note": "需要 ChatGPT Plus saved login。在终端 `codex login` 完成登录后回到本页刷新。",
        },
        {
            "backend": "codebuddy",
            "label": "CodeBuddy Code（中国站）",
            "cli_available": codebuddy_version is not None,
            "version": codebuddy_version,
            "cli_path": codebuddy_path,
            "logged_in": codebuddy_logged_in,
            "login_probe": auth_visibility,
            "login_capable": codebuddy_path is not None,
            "login_command": "点击下方「一键登录授权」按钮，后台打开中国站授权页",
            "login_status_hint": (
                "当前控制台进程无权读取 Windows 用户级认证目录；请从桌面正常权限启动控制台后重新探测。"
                if auth_visibility == "unreadable"
                else (
                    "未发现可确认的 CodeBuddy CLI 登录标记，请完成中国站授权后重新探测。"
                    if codebuddy_logged_in is None
                    else (
                        "检测到 CLI 登录态文件。"
                        if codebuddy_logged_in
                        else "未检测到登录态，请点击一键登录授权。"
                    )
                )
            ),
            "note": "需要中国站 internal 环境登录态。CLI 已随项目安装在 .agent-hub/tools；授权页会由后台 SDK 自动打开，完成后回本页重新探测。",
        },
        {
            "backend": "fake",
            "label": "Fake（离线演示）",
            "cli_available": True,
            "version": "built-in",
            "cli_path": None,
            "logged_in": True,
            "login_capable": False,
            "login_command": None,
            "login_status_hint": None,
            "note": "无需账号，用于离线跑通流程与界面演示。",
        },
    ]
