"""T7：干净 Windows 环境 bootstrap（30 分钟安装演示）。

核心逻辑保持纯函数可测；CLI 入口见 ``scripts/bootstrap.py``。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

MIN_PYTHON = (3, 10)


def python_version_ok(version: str) -> bool:
    """解析 "3.12.5" 并判断是否 >= 要求版本。"""
    try:
        major, minor = (int(part) for part in version.split(".")[:2])
    except (ValueError, AttributeError):
        return False
    return (major, minor) >= MIN_PYTHON


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


def check_prerequisites() -> list[dict[str, object]]:
    """返回前置检查清单，每项含 name/required/ok/version/hint。"""
    python = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    git_version = _tool_version("git", "--version")
    codex_version = _tool_version("codex", "--version")
    codebuddy_version = _tool_version("codebuddy", "--version")
    return [
        {
            "name": "python",
            "required": True,
            "ok": python_version_ok(python),
            "version": python,
            "hint": f"需要 Python >= 3.{MIN_PYTHON[1]}",
        },
        {
            "name": "git",
            "required": True,
            "ok": git_version is not None,
            "version": git_version,
            "hint": "需要 Git（https://git-scm.com/download/win）",
        },
        {
            "name": "codex",
            "required": False,
            "ok": codex_version is not None,
            "version": codex_version,
            "hint": "可选：Codex CLI（需要 ChatGPT Plus saved login）",
        },
        {
            "name": "codebuddy",
            "required": False,
            "ok": codebuddy_version is not None,
            "version": codebuddy_version,
            "hint": "可选：CodeBuddy CLI（中国站 internal）",
        },
    ]


def create_venv(venv_dir: Path) -> bool:
    """创建虚拟环境（已存在则跳过），返回是否新建。"""
    if venv_dir.exists():
        return False
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        check=True,
        timeout=120,
    )
    return True


def install_dependencies(venv_dir: Path, project_root: Path) -> None:
    """在 venv 内安装本项目（含 openai-codex/codebuddy-agent-sdk 依赖）。"""
    pip = venv_dir / ("Scripts" if sys.platform == "win32" else "bin") / "pip"
    subprocess.run(
        [str(pip), "install", "--quiet", "-e", str(project_root)],
        check=True,
        timeout=600,
    )


def initialize_workspace(cwd: Path) -> list[Path]:
    """创建 .agent-hub 目录结构，返回创建的目录。"""
    hub = cwd / ".agent-hub"
    created: list[Path] = []
    for name in ("state", "reports", "backups", "certs", "logs"):
        directory = hub / name
        if not directory.exists():
            directory.mkdir(parents=True)
            created.append(directory)
    return created


def bootstrap(
    project_root: Path,
    *,
    dry_run: bool = False,
) -> dict[str, object]:
    """执行完整 bootstrap，返回汇总。"""
    venv_dir = project_root / ".venv"
    prereqs = check_prerequisites()
    required_missing = [
        item for item in prereqs if item["required"] and not item["ok"]
    ]
    result: dict[str, object] = {
        "prerequisites": prereqs,
        "required_ok": not required_missing,
    }
    if required_missing:
        return result
    if dry_run:
        return result
    created_venv = create_venv(venv_dir)
    install_dependencies(venv_dir, project_root)
    created_dirs = initialize_workspace(project_root)
    result.update(
        {
            "venv": str(venv_dir),
            "venv_created": created_venv,
            "workspace_dirs": [str(path) for path in created_dirs],
            "status": "ready",
        }
    )
    return result
