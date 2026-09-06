"""网页控制台：本地设置、Team 配置持久化与连接探测。

- 设置与保存的 team 只放 ``.agent-hub/``（状态目录）。
- 不存储任何 token / cookie / credential：Codex 用 saved login、CodeBuddy 用
  CLI 登录态，这里只做探测与登录引导。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

DEFAULT_SETTINGS: dict[str, Any] = {
    "default_team": "config/team.yaml",
    "default_backend": "fake",
}


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
        teams.append({"team_id": "default", "source": str(default), "path": str(default)})
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
    info_files = [
        p
        for p in entries
        if p.name.endswith(".info")
        and ".logged-out" not in p.name
        and p.stat().st_size > 0
    ]
    if logged_out:
        return False
    if info_files:
        return True
    return None


def _login_window_script(project_root: Path, backend: str) -> Path:
    """生成后端登录引导脚本（.agent-hub/login/<backend>-login.cmd）。

    脚本本身只做三件事：设好环境变量、启动 CLI 登录流程、暂停等待用户查看。
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
            "set CODEBUDDY_SKIP_GIT_BASH_CHECK=1\r\n"
            "set CODEBUDDY_INTERNET_ENVIRONMENT=internal\r\n"
            "echo ============================================================\r\n"
            "echo   CodeBuddy login helper\r\n"
            "echo   When the CodeBuddy session starts below, type:  /login\r\n"
            "echo   then press Enter - a browser will open for authorization.\r\n"
            "echo   After it says signed in, close this window and refresh.\r\n"
            "echo ============================================================\r\n"
            f'"{cli}"\r\n'
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
    """在桌面打开后端登录引导窗口（dry_run=True 只生成脚本不弹窗，供测试）。

    返回 dict：ok / message / script（生成的引导脚本路径）。
    """
    try:
        script = _login_window_script(project_root, backend)
    except (FileNotFoundError, ValueError) as exc:
        return {"ok": False, "message": str(exc), "script": None}
    if dry_run:
        return {"ok": True, "message": "script ready", "script": str(script)}
    try:
        import subprocess
        import sys

        if sys.platform == "win32":
            # start 一个新控制台窗口运行引导脚本（窗口标题避免中文避免 GBK 乱码）
            subprocess.Popen(
                ["cmd", "/c", "start", "AgentHub-login", str(script)],
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
            )
        else:
            subprocess.Popen(
                ["cmd", "/k", str(script)],
                start_new_session=True,
            )
    except OSError as exc:  # 打不开窗口（如沙箱/无桌面会话）
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
    codebuddy_logged_in = codebuddy_login_state() if codebuddy_path else None

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
            "login_capable": codebuddy_path is not None,
            "login_command": "点击下方「一键登录授权」按钮打开登录窗口，输入 /login 完成浏览器授权",
            "login_status_hint": (
                "CLI 独立认证，与 WorkBuddy 桌面客户端不共享。"
                if codebuddy_logged_in is None
                else (
                    "检测到 CLI 登录态文件。"
                    if codebuddy_logged_in
                    else "未检测到登录态，请点击一键登录授权。"
                )
            ),
            "note": "需要中国站 internal 环境登录态。CLI 已随项目安装在 .agent-hub/tools；登录在独立窗口内用 /login 完成。",
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
