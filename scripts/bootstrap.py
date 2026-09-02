"""T7：干净 Windows 环境 bootstrap CLI（30 分钟安装演示）。

用法：
    & '.venv\\Scripts\\python.exe' scripts/bootstrap.py --check        # 只检查前置
    & '.venv\\Scripts\\python.exe' scripts/bootstrap.py                # 完整安装
    & '.venv\\Scripts\\python.exe' scripts/bootstrap.py --root D:\\projects\\agent-hub
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from orchestrator.bootstrapper import bootstrap, check_prerequisites, initialize_workspace


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--check", action="store_true", help="只检查前置，不安装")
    parser.add_argument("--dry-run", action="store_true", help="不实际安装")
    args = parser.parse_args(argv)

    if args.check:
        prereqs = check_prerequisites()
        print(json.dumps(prereqs, ensure_ascii=False, indent=2))
        missing = [item for item in prereqs if item["required"] and not item["ok"]]
        return 1 if missing else 0

    result = bootstrap(args.root, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("required_ok"):
        print("前置检查未通过：请先安装缺失的必需工具。")
        return 1
    if result.get("status") == "ready":
        print(
            "bootstrap 完成。下一步：\n"
            f"  1) & '{result['venv']}\\Scripts\\python.exe' -m orchestrator init\n"
            "  2) & '.venv\\Scripts\\python.exe' -m orchestrator console --run <run-id> --port 8080"
        )
        return 0
    return 0 if args.dry_run else 1


if __name__ == "__main__":
    raise SystemExit(main())
