"""R8 个人版真实端到端验收门禁。

这个脚本只读取验收证据，不启动模型、不修改项目 checkout，也不执行删除或
合并。``stage3-real.json`` 是早期 20 场景证据，不能直接替代 R8 的 A--J
验收；没有完整证据时，脚本会输出 ``checkpoint``，而不是把历史 PASS 误报为
个人版完成。

用法（只核对当前已有证据）：

    .venv\\Scripts\\python.exe scripts/personal_r8_acceptance.py

未来真实验收完成后，把符合 ``personal-r8-v1`` 契约的证据 JSON 传入：

    .venv\\Scripts\\python.exe scripts/personal_r8_acceptance.py \\
        --evidence .agent-hub/reports/personal-r8-evidence.json

退出码为 0 只表示 A--J 全部满足；checkpoint/失败返回 1，便于脚本和 CI
阻止误发布。报告始终写入 ``.agent-hub``，不进入 Git。
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
FORMAT_VERSION = "personal-r8-v1"
SAMPLE_ID = "personal-r8-fixed-sample-v1"


SCENARIO_SPECS: dict[str, dict[str, Any]] = {
    "A": {
        "title": "主管 + 双 Worker 返回 1/2",
        "minimum_repetitions": 3,
        "required": (
            "supervisor_summary_refs",
            "worker_calls",
            "agent_sessions",
        ),
    },
    "B": {
        "title": "并行修改两个独立模块",
        "minimum_repetitions": 3,
        "required": (
            "overlap_observed",
            "distinct_worktrees",
            "distinct_commits",
            "user_checkout_unchanged",
        ),
    },
    "C": {
        "title": "依赖任务读取已验证版本",
        "minimum_repetitions": 1,
        "required": (
            "dependency_base_commits",
            "verified_upstream_refs",
        ),
    },
    "D": {
        "title": "独立审核发现缺陷并有限返工",
        "minimum_repetitions": 3,
        "required": (
            "reviewer_detected_defect",
            "rework_attempt",
            "post_rework_verification",
        ),
    },
    "E": {
        "title": "格式错误与非法计划",
        "minimum_repetitions": 1,
        "required": (
            "format_repair_count",
            "out_of_scope_side_effects",
            "fake_complete",
        ),
    },
    "F": {
        "title": "失败恢复与取消",
        "minimum_repetitions": 1,
        "required": (
            "restart_duplicate_dispatches",
            "duplicate_merges",
            "cancel_state_explicit",
        ),
    },
    "G": {
        "title": "运行中用户引导",
        "minimum_repetitions": 1,
        "required": (
            "guidance_target",
            "delivery_status",
            "applied_at",
            "scope_unchanged",
        ),
    },
    "H": {
        "title": "预算上限",
        "minimum_repetitions": 1,
        "required": (
            "budget_cap_enforced",
            "new_work_after_cap",
            "remaining_reported",
        ),
    },
    "I": {
        "title": "审核和集成",
        "minimum_repetitions": 1,
        "required": (
            "evidence_visible",
            "old_evidence_rejected",
            "integration_checks_passed",
        ),
    },
    "J": {
        "title": "单 Agent 对照",
        "minimum_repetitions": 1,
        "required": (
            "same_input_comparison",
            "usage_disclosure",
            "manual_interventions_recorded",
        ),
    },
}

_SECRET_KEY = re.compile(
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|authorization|cookie|session[_-]?token)",
    re.IGNORECASE,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _safe_copy(value: Any, *, redactions: list[str], path: str = "") -> Any:
    """复制证据并丢弃可能包含凭据的字段。

    真实报告可能由第三方 SDK 生成；即使调用方误把敏感字段写进证据，R8
    报告也不应把它原样写入项目目录或终端输出。
    """

    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            current = f"{path}.{key_text}" if path else key_text
            if _SECRET_KEY.search(key_text):
                redactions.append(current)
                continue
            result[key_text] = _safe_copy(item, redactions=redactions, path=current)
        return result
    if isinstance(value, list):
        return [
            _safe_copy(item, redactions=redactions, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, tuple):
        return [
            _safe_copy(item, redactions=redactions, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    return value


def _truthy(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.lower() in {"true", "pass", "passed"})


def _safe_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _stage3_history(stage3_report: dict[str, Any] | None) -> dict[str, Any]:
    if not stage3_report:
        return {
            "available": False,
            "usable_as_r8": False,
            "reason": "缺少历史 stage3-real 报告",
        }
    total = _safe_int(stage3_report.get("scenarios_total")) or 0
    passed = _safe_int(stage3_report.get("passed")) or 0
    backends = sorted(
        {
            str(item.get("backend"))
            for item in (stage3_report.get("results") or {}).values()
            if isinstance(item, dict) and item.get("backend")
        }
    )
    return {
        "available": True,
        "usable_as_r8": False,
        "run_id": str(stage3_report.get("run_id") or ""),
        "status": str(stage3_report.get("status") or ""),
        "scenarios_total": total,
        "passed": passed,
        "backends": backends,
        "reason": (
            "历史 E2 报告仅覆盖预冻结 20 场景；缺少 R8 A-J 的真实主管、独立审核、"
            "返工、重启/取消、用户引导和单 Agent 对照证据"
        ),
    }


def _missing_or_false(
    entry: dict[str, Any], required: tuple[str, ...]
) -> tuple[list[str], list[str]]:
    missing: list[str] = []
    failed: list[str] = []
    for field in required:
        if field not in entry:
            missing.append(field)
            continue
        value = entry[field]
        if field == "format_repair_count":
            count = _safe_int(value)
            if count is None or count > 1:
                failed.append(field)
        elif field in {
            "out_of_scope_side_effects",
            "restart_duplicate_dispatches",
            "duplicate_merges",
            "new_work_after_cap",
        }:
            count = _safe_int(value)
            if count is None:
                missing.append(field)
            elif count != 0:
                failed.append(field)
        elif field == "fake_complete":
            if _truthy(value):
                failed.append(field)
        elif isinstance(value, (list, tuple, dict, str)) and not value:
            missing.append(field)
        elif value is False:
            failed.append(field)
    return missing, failed


def _evaluate_scenario(
    scenario_id: str, raw: Any, *, safe_refs: bool = True
) -> dict[str, Any]:
    spec = SCENARIO_SPECS[scenario_id]
    if not isinstance(raw, dict):
        return {
            "scenario": scenario_id,
            "title": spec["title"],
            "status": "PENDING",
            "reason": "缺少结构化 R8 场景记录",
            "repetitions": 0,
            "required_repetitions": spec["minimum_repetitions"],
        }
    repetitions = _safe_int(raw.get("repetitions")) or 0
    refs = raw.get("evidence_refs")
    missing, failed = _missing_or_false(raw, spec["required"])
    if not isinstance(refs, list) or not refs or not all(isinstance(ref, str) and ref for ref in refs):
        missing.append("evidence_refs")
    if repetitions < spec["minimum_repetitions"]:
        missing.append("repetitions")
    if failed:
        status = "FAIL"
        reason = "不满足安全/正确性约束：" + ", ".join(sorted(set(failed)))
    elif missing:
        status = "PENDING"
        reason = "缺少 R8 证据：" + ", ".join(sorted(set(missing)))
    else:
        status = "PASS"
        reason = "结构化证据满足该场景出口"
    result = {
        "scenario": scenario_id,
        "title": spec["title"],
        "status": status,
        "reason": reason,
        "repetitions": repetitions,
        "required_repetitions": spec["minimum_repetitions"],
        "evidence_refs": list(refs) if isinstance(refs, list) else [],
    }
    if not safe_refs:
        result["raw_fields_checked"] = list(spec["required"])
    return result


def evaluate_evidence(
    evidence: dict[str, Any] | None,
    *,
    stage3_report: dict[str, Any] | None = None,
    source: str = "none",
) -> dict[str, Any]:
    """评估一份 R8 证据，返回不含凭据的可保存报告。"""

    raw = evidence if isinstance(evidence, dict) else {}
    redactions: list[str] = []
    safe = _safe_copy(raw, redactions=redactions)
    if not isinstance(safe, dict):
        safe = {}
    backend_mode = str(safe.get("backend_mode") or "")
    backends_value = safe.get("backends")
    backends = sorted(
        {
            str(item.get("backend") if isinstance(item, dict) else item)
            for item in (backends_value if isinstance(backends_value, list) else [])
            if (item.get("backend") if isinstance(item, dict) else item)
        }
    )
    top_level_missing: list[str] = []
    if safe.get("format_version") != FORMAT_VERSION:
        top_level_missing.append("format_version")
    if safe.get("sample_id") != SAMPLE_ID:
        top_level_missing.append("sample_id")
    if backend_mode != "real":
        top_level_missing.append("backend_mode=real")
    if not {"codex", "codebuddy"}.issubset(backends):
        top_level_missing.append("backends=codex+codebuddy")
    scenarios = safe.get("scenarios")
    scenario_records: dict[str, Any] = {}
    for scenario_id in SCENARIO_SPECS:
        raw_entry = scenarios.get(scenario_id) if isinstance(scenarios, dict) else None
        scenario_records[scenario_id] = _evaluate_scenario(scenario_id, raw_entry)
    statuses = [item["status"] for item in scenario_records.values()]
    if any(status == "FAIL" for status in statuses):
        status = "FAIL"
    elif top_level_missing or any(status != "PASS" for status in statuses):
        status = "CHECKPOINT"
    else:
        status = "COMPLETE"
    report: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "generated_at": _now(),
        "source": source,
        "status": status,
        "credential_free": True,
        "redacted_fields": redactions,
        "sample_id": safe.get("sample_id") or SAMPLE_ID,
        "run_id": safe.get("run_id") or "",
        "backend_mode": backend_mode or "unknown",
        "backends": backends,
        "top_level_missing": top_level_missing,
        "scenarios": scenario_records,
        "historical_stage3": _stage3_history(stage3_report),
        "notes": [
            "历史 stage3-real 报告不能替代 R8 A-J 真实证据。",
            "Token/订阅金额不可得时必须在 J.usage_disclosure 中明确披露，不能按零消耗计算。",
            "报告没有执行模型调用；真实证据由调用方在固定样例项目中产生。",
        ],
    }
    return report


def _default_paths(root: Path) -> tuple[Path, Path]:
    return (
        root / ".agent-hub" / "reports" / "stage3-real.json",
        root / ".agent-hub" / "reports" / "personal-r8.json",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    default_stage3, default_output = _default_paths(ROOT)
    parser.add_argument("--evidence", type=Path, default=None, help="personal-r8-v1 证据 JSON")
    parser.add_argument(
        "--stage3-report", type=Path, default=default_stage3, help="旧 20 场景报告（仅历史参考）"
    )
    parser.add_argument("--output", type=Path, default=default_output)
    args = parser.parse_args(argv)

    stage3_path = args.stage3_report
    if not stage3_path.is_absolute():
        stage3_path = ROOT / stage3_path
    stage3_report = _read_json(stage3_path)
    evidence: dict[str, Any] | None = None
    source = "none"
    if args.evidence is not None:
        evidence_path = args.evidence if args.evidence.is_absolute() else ROOT / args.evidence
        evidence = _read_json(evidence_path)
        source = str(evidence_path)
    else:
        source = "historical-stage3-only"
    report = evaluate_evidence(evidence, stage3_report=stage3_report, source=source)
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "COMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
