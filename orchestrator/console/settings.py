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


def _tool_version(executable: str, *args: str) -> str | None:
    try:
        result = subprocess.run(
            [executable, *args],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return result.stdout.strip().splitlines()[0].strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def probe_connections() -> list[dict[str, Any]]:
    """探测各后端的 CLI/登录可达性（只探测不存储任何秘密）。"""
    codex_cli = _tool_version("codex", "--version")
    codebuddy_cli = _tool_version("codebuddy", "--version")
    return [
        {
            "backend": "codex",
            "label": "OpenAI Codex",
            "cli_available": codex_cli is not None,
            "version": codex_cli,
            "login_command": "codex login",
            "note": "需要 ChatGPT Plus saved login；登录后在终端执行命令，然后回到本页刷新。",
        },
        {
            "backend": "codebuddy",
            "label": "CodeBuddy Code（中国站）",
            "cli_available": codebuddy_cli is not None,
            "version": codebuddy_cli,
            "login_command": "codebuddy login",
            "note": "需要中国站 internal 环境登录态。",
        },
        {
            "backend": "fake",
            "label": "Fake（离线演示）",
            "cli_available": True,
            "version": "built-in",
            "login_command": None,
            "note": "无需账号，用于离线跑通流程。",
        },
    ]
