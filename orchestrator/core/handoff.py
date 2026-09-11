"""Versioned task handoff and structured result contracts.

The Hub owns paths, permissions, budgets, and references.  A model may only
contribute the logical task request; it cannot replace the values in this
package with an arbitrary local path or a claim about another task.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping


HANDOFF_CONTRACT_VERSION = 1


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value


@dataclass(frozen=True)
class HandoffPackage:
    contract_version: int
    project_id: str
    run_id: str
    task_id: str
    attempt_id: str
    plan_revision: int
    goal: str
    accepted_facts: tuple[Mapping[str, Any], ...] = ()
    constraints: tuple[str, ...] = ()
    input_refs: tuple[Any, ...] = ()
    base_commit: str | None = None
    write_scope: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    budget: Any = None
    expected_output: Any = None
    agent_id: str = ""
    role_id: str = ""
    backend: str = ""
    model: str | None = None
    provider_id: str | None = None
    session_ref_id: str = ""

    def __post_init__(self) -> None:
        for name in ("project_id", "run_id", "task_id", "attempt_id", "goal"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} must not be empty")
        if self.contract_version != HANDOFF_CONTRACT_VERSION:
            raise ValueError("unsupported handoff contract version")
        if self.plan_revision < 1:
            raise ValueError("plan_revision must be at least 1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "plan_revision": self.plan_revision,
            "goal": self.goal,
            "accepted_facts": [_jsonable(item) for item in self.accepted_facts],
            "constraints": list(self.constraints),
            "input_refs": _jsonable(self.input_refs),
            "base_commit": self.base_commit,
            "write_scope": list(self.write_scope),
            "acceptance_criteria": list(self.acceptance_criteria),
            "budget": _jsonable(self.budget),
            "expected_output": _jsonable(self.expected_output),
            "agent_id": self.agent_id,
            "role_id": self.role_id,
            "backend": self.backend,
            "model": self.model,
            "provider_id": self.provider_id,
            "session_ref_id": self.session_ref_id,
        }

    @property
    def digest(self) -> str:
        canonical = json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


def render_handoff_prompt(instruction: str, package: HandoffPackage) -> str:
    """Give the adapter the Hub-owned facts without making them mutable input."""
    return (
        f"{instruction}\n\n"
        "The following handoff package is authoritative. Do not replace its "
        "paths, permissions, budget, task identity, or references.\n"
        "```json\n"
        + json.dumps(package.to_dict(), ensure_ascii=False, sort_keys=True, indent=2)
        + "\n```"
    )


def validate_result_payload(
    payload: Mapping[str, Any], *, run_id: str, task_id: str, attempt_id: str
) -> dict[str, Any]:
    """Normalize a worker result and reject cross-task or unstructured claims."""
    result = dict(payload)
    for key, expected in (
        ("run_id", run_id),
        ("task_id", task_id),
        ("attempt_id", attempt_id),
    ):
        if key in result and str(result[key]) != expected:
            raise ValueError(f"worker result {key} does not match its attempt")
    status = str(result.get("status") or "completed").strip().lower()
    if status not in {"completed", "failed", "needs_input", "blocked"}:
        raise ValueError("worker result status must be completed, failed, needs_input, or blocked")
    result["run_id"] = run_id
    result["task_id"] = task_id
    result["attempt_id"] = attempt_id
    result["status"] = status
    refs = result.get("artifact_refs", ())
    if not isinstance(refs, (list, tuple)):
        raise ValueError("artifact_refs must be an array")
    result["artifact_refs"] = list(refs)
    checks = result.get("claimed_checks", ())
    if not isinstance(checks, (list, tuple)):
        raise ValueError("claimed_checks must be an array")
    result["claimed_checks"] = list(checks)
    issues = result.get("remaining_issues", ())
    if not isinstance(issues, (list, tuple)):
        raise ValueError("remaining_issues must be an array")
    result["remaining_issues"] = list(issues)
    return result


def result_digest(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        _jsonable(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()

