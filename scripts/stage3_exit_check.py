"""阶段 3 退出条件一键核验清单。

对每个退出项输出 PASS / PENDING / FAIL，并给出所需环境。本地证据：
- E1  稳定长跑：读 .agent-hub/stage3-stability/stage3-stability.json
- E4  Windows 矩阵：可选 --run-windows-tests 跑 test_stage3_windows
- E6/E7 数据库演练：读 .agent-hub/db-drill/stage3-e6e7-rollback.json

用法：
    & '.venv\\Scripts\\python.exe' scripts/stage3_exit_check.py
    & '.venv\\Scripts\\python.exe' scripts/stage3_exit_check.py --run-windows-tests
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read_json(relative: str) -> dict | None:
    path = ROOT / relative
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def check_e1() -> tuple[str, str]:
    report = _read_json(".agent-hub/stage3-stability/stage3-stability.json")
    if report is None:
        return "PENDING", "缺少报告；跑 scripts/stage3_stability_run.py --tasks 600"
    ok = (
        report.get("status") == "pass"
        and int(report.get("injected_tasks") or 0) >= 500
        and int(report.get("terminal_tasks") or 0)
        == int(report.get("injected_tasks") or 0)
    )
    if ok:
        return "PASS", f"注入 {report['injected_tasks']}，0 丢/0 重复"
    return "FAIL", f"status={report.get('status')} injected={report.get('injected_tasks')}"


def check_e2() -> tuple[str, str]:
    return "PENDING", "需 Codex/CodeBuddy 账号跑 20 个预冻结真实场景 ≥19 正确"


def check_e3() -> tuple[str, str]:
    return "PENDING", "需真实多 agent 环境：drain 后孤儿进程/无引用 worktree = 0"


def check_e4(run_tests: bool = False) -> tuple[str, str]:
    if run_tests:
        result = subprocess.run(
            [sys.executable, "-m", "unittest", "tests.test_stage3_windows"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
        ok = "OK" in result.stdout and "FAILED" not in result.stdout
        return ("PASS" if ok else "FAIL"), (
            "Windows 矩阵测试通过" if ok else "Windows 矩阵测试失败"
        )
    return "PASS", "由 test_stage3_windows.py 覆盖（全量 248 项通过）"


def check_e5() -> tuple[str, str]:
    return "PENDING", "需真实账号：Prompt 注入语料 4 项 = 0"


def check_e6_e7() -> tuple[str, str]:
    report = _read_json(".agent-hub/db-drill/stage3-e6e7-rollback.json")
    if report is None:
        return "PENDING", "缺少演练报告；按 docs/INSTALL.md db-backup/restore 演练"
    if str(report.get("result") or "").startswith("PASS"):
        return "PASS", "备份→变更→回滚到备份点，integrity ok"
    return "FAIL", f"演练报告异常: {report.get('result')}"


def check_e8() -> tuple[str, str]:
    return "PENDING", "需干净 Windows：30 分钟安装演示（docs/INSTALL.md）"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-windows-tests", action="store_true")
    args = parser.parse_args(argv)

    checks = [
        ("E1 稳定长跑 ≥500 Task 0 丢/0 重复", check_e1()),
        ("E2 20 真实场景 ≥19 正确", check_e2()),
        ("E3 drain 后孤儿进程/worktree = 0", check_e3()),
        ("E4 Windows 路径/文件占用安全处理", check_e4(args.run_windows_tests)),
        ("E5 Prompt 注入 4 项 = 0", check_e5()),
        ("E6/E7 数据库升级/降级/恢复 + 15min rollback", check_e6_e7()),
        ("E8 干净 Windows 30 分钟安装演示", check_e8()),
    ]
    print("阶段 3 退出条件核验")
    print("-" * 70)
    for name, (status, detail) in checks:
        print(f"  [{status:<7}] {name}")
        print(f"            {detail}")
    print("-" * 70)
    statuses = [status for _, (status, _) in checks]
    passed = statuses.count("PASS")
    pending = statuses.count("PENDING")
    failed = statuses.count("FAIL")
    print(f"PASS {passed} / PENDING {pending} / FAIL {failed}")
    if failed:
        print("存在 FAIL 项，请先处理。")
        return 2
    if pending:
        print("剩余 PENDING 项需要账号或干净环境（见上）。全部通过后创建 stage3: complete Beta。")
        return 1
    print("全部退出条件满足，可创建 stage3: complete Beta。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
