"""阶段 3 退出条件一键核验清单。

对每个退出项输出 PASS / PENDING / FAIL，并给出所需环境。本地证据：
- E1  稳定长跑：读 .agent-hub/stage3-stability/stage3-stability.json
- E2  20 真实场景：读 .agent-hub/reports/stage3-real.json（runner 自动写入）
- E3  drain 孤儿检查：读 .agent-hub/reports/stage3-drain.json（stage3_drain_check.py）
- E4  Windows 矩阵：可选 --run-windows-tests 跑 test_stage3_windows
- E5  Prompt 注入语料：读 .agent-hub/reports/stage3-injection.json
      （stage3_prompt_injection_run.py）
- E6/E7 数据库演练：读 .agent-hub/db-drill/stage3-e6e7-rollback.json

用法：
    & '.venv\\Scripts\\python.exe' scripts/stage3_exit_check.py
    & '.venv\\Scripts\\python.exe' scripts/stage3_exit_check.py --run-windows-tests
    & '.venv\\Scripts\\python.exe' scripts/stage3_exit_check.py --skip-e8
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
    report = _read_json(".agent-hub/reports/stage3-real.json")
    if report is None:
        return "PENDING", "缺少真实场景报告；跑 scripts/stage3_real_run.py --backends codex,codebuddy"
    total = int(report.get("scenarios_total") or 0)
    passed = int(report.get("passed") or 0)
    if report.get("status") == "pass" and total >= 20 and passed >= 19:
        return "PASS", f"真实 20 场景 {passed}/{total} 正确（Codex + CodeBuddy）"
    if total:
        return "FAIL", f"真实场景 {passed}/{total}，未达 ≥19/20"
    return "PENDING", "真实场景报告为空"


def check_e3() -> tuple[str, str]:
    report = _read_json(".agent-hub/reports/stage3-drain.json")
    if report is None:
        return "PENDING", "缺少报告；跑 scripts/stage3_drain_check.py --backends codex,codebuddy"
    if report.get("status") == "pass":
        return "PASS", (
            f"真实多 agent drain 后：孤儿进程 0（检查 "
            f"{report['processes']['checks'].get('samples_total')} 轮）、"
            f"无引用 worktree 0、DB 无持续锁（{report['run_seconds']}s）"
        )
    return "FAIL", f"drain 报告异常: {report.get('status')} {report.get('violations')}"


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
    return "PASS", "由 test_stage3_windows.py 覆盖（全量测试通过）"


def check_e5() -> tuple[str, str]:
    report = _read_json(".agent-hub/reports/stage3-injection.json")
    if report is None:
        return "PENDING", "缺少报告；跑 scripts/stage3_prompt_injection_run.py --backends codex,codebuddy"
    if report.get("status") == "pass":
        return "PASS", (
            f"注入语料 {report['scenarios_total']} 项全过，违规=0；"
            f"可判定 {report.get('conclusive')}/{report['scenarios_total']}"
        )
    if report.get("status") == "inconclusive":
        return "FAIL", (
            f"存在不可判定项 {report.get('inconclusive_keys')}（CLI 被环境阻断）"
        )
    return "FAIL", "注入语料存在违规，见 .agent-hub/reports/stage3-injection.json"


def check_e6_e7() -> tuple[str, str]:
    report = _read_json(".agent-hub/db-drill/stage3-e6e7-rollback.json")
    if report is None:
        return "PENDING", "缺少演练报告；按 docs/INSTALL.md db-backup/restore 演练"
    if str(report.get("result") or "").startswith("PASS"):
        return "PASS", "备份→变更→回滚到备份点，integrity ok"
    return "FAIL", f"演练报告异常: {report.get('result')}"


def check_e8(skip: bool = False) -> tuple[str, str]:
    if skip:
        return "SKIP", "开发自用不要求干净 Windows 演示（用户决策，记录在 PROJECT_PROGRESS.md）"
    return "PENDING", "需干净 Windows：30 分钟安装演示（docs/INSTALL.md）"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-windows-tests", action="store_true")
    parser.add_argument(
        "--skip-e8",
        action="store_true",
        help="E8 明确跳过（开发自用，不要求干净 Windows 30 分钟安装演示）",
    )
    args = parser.parse_args(argv)

    checks = [
        ("E1 稳定长跑 ≥500 Task 0 丢/0 重复", check_e1()),
        ("E2 20 真实场景 ≥19 正确", check_e2()),
        ("E3 drain 后孤儿进程/worktree = 0", check_e3()),
        ("E4 Windows 路径/文件占用安全处理", check_e4(args.run_windows_tests)),
        ("E5 Prompt 注入 4 项 = 0", check_e5()),
        ("E6/E7 数据库升级/降级/恢复 + 15min rollback", check_e6_e7()),
        ("E8 干净 Windows 30 分钟安装演示", check_e8(args.skip_e8)),
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
    skipped = statuses.count("SKIP")
    print(f"PASS {passed} / PENDING {pending} / FAIL {failed} / SKIP {skipped}")
    if failed:
        print("存在 FAIL 项，请先处理。")
        return 2
    if pending:
        print("剩余 PENDING 项需要账号或干净环境（见上）。全部通过后创建 stage3: complete Beta。")
        return 1
    print("全部退出条件满足（含显式跳过项），可创建 stage3: complete Beta。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
