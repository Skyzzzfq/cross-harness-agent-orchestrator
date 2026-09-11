"""Versioned role templates and deterministic prompt assembly.

Role templates describe intent and contracts.  They never grant filesystem or
backend permissions; those remain in the persisted team/pool and task policy.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


_DEFAULTS: dict[str, dict[str, Any]] = {
    "supervisor": {
        "version": 1,
        "title": "Supervisor",
        "goal": "Turn one user request into a bounded, reviewable execution plan.",
        "responsibilities": (
            "clarify the goal and constraints",
            "split independent work without exceeding the task budget",
            "return a versioned JSON plan for approval",
            "summarize verified child results",
        ),
        "forbidden_actions": (
            "claim a child task is complete without its result",
            "expand write_scope or provider permissions",
            "execute code directly when only planning is requested",
        ),
        "input_contract": (
            "user_goal",
            "project_rules",
            "role_and_pool_capabilities",
            "budget",
        ),
        "output_contract": (
            "plan_version",
            "revision",
            "summary",
            "worker_concurrency",
            "task_cap",
            "tasks",
        ),
        "tool_policy": ("read_project_metadata", "read_task_history"),
        "context_policy": (
            "include approved facts and references",
            "do not include unrelated agent transcripts",
        ),
        "budget_defaults": {"max_tasks": 8, "max_calls": 16},
    },
    "worker": {
        "version": 1,
        "title": "Implementation Worker",
        "goal": "Complete one bounded task and publish verifiable output.",
        "responsibilities": (
            "read only the supplied handoff and allowed inputs",
            "work inside the assigned cwd and write_scope",
            "report checks, artifacts, and remaining issues",
        ),
        "forbidden_actions": (
            "modify another task's worktree",
            "change the approved task scope",
            "declare review or integration complete",
        ),
        "input_contract": (
            "task_handoff",
            "accepted_facts",
            "input_refs",
            "acceptance_criteria",
        ),
        "output_contract": (
            "status",
            "summary",
            "candidate_commit",
            "artifact_refs",
            "claimed_checks",
            "remaining_issues",
        ),
        "tool_policy": ("read_allowed_inputs", "write_assigned_scope", "run_checks"),
        "context_policy": (
            "new task gets a new context",
            "resume only the same attempt/session",
        ),
        "budget_defaults": {"max_attempts": 2, "timeout_seconds": 120},
    },
    "implementation-worker": {
        "version": 1,
        "title": "Implementation Worker",
        "goal": "Complete one bounded task and publish verifiable output.",
        "responsibilities": (
            "read only the supplied handoff and allowed inputs",
            "work inside the assigned cwd and write_scope",
            "report checks, artifacts, and remaining issues",
        ),
        "forbidden_actions": (
            "modify another task's worktree",
            "change the approved task scope",
            "declare review or integration complete",
        ),
        "input_contract": ("task_handoff", "accepted_facts", "input_refs", "acceptance_criteria"),
        "output_contract": ("status", "summary", "candidate_commit", "artifact_refs", "claimed_checks", "remaining_issues"),
        "tool_policy": ("read_allowed_inputs", "write_assigned_scope", "run_checks"),
        "context_policy": ("new task gets a new context", "resume only the same attempt/session"),
        "budget_defaults": {"max_attempts": 2, "timeout_seconds": 120},
    },
    "reviewer": {
        "version": 1,
        "title": "Independent Reviewer",
        "goal": "Compare a candidate with the acceptance contract using independent evidence.",
        "responsibilities": (
            "inspect the candidate commit and acceptance criteria",
            "run or inspect allowed verification evidence",
            "return pass, rework, or blocked with locator-level reasons",
        ),
        "forbidden_actions": (
            "modify the candidate code",
            "inherit the author's unverified claims as evidence",
            "lower an acceptance requirement",
        ),
        "input_contract": ("original_goal", "candidate_commit", "acceptance_criteria", "evidence_refs"),
        "output_contract": ("decision", "criteria_results", "evidence_refs", "repair_conditions"),
        "tool_policy": ("read_candidate", "run_fixed_checks", "write_review_report"),
        "context_policy": ("independent context", "no author transcript by default"),
        "budget_defaults": {"max_attempts": 1, "timeout_seconds": 120},
    },
}


def role_template_for(role_id: str) -> dict[str, Any] | None:
    template = _DEFAULTS.get(str(role_id).strip())
    return None if template is None else dict(template)


def role_binding_blockers(team_spec: Any) -> list[str]:
    """Return actionable role/pool problems without exposing credentials."""
    blockers: list[str] = []
    roles = {str(role.role_id): role for role in team_spec.roles}
    if str(team_spec.bootstrap_supervisor) not in roles:
        blockers.append(f"missing supervisor role: {team_spec.bootstrap_supervisor}")
    for pool in team_spec.agent_pools:
        role_id = str(pool.role_id)
        if role_id not in roles:
            blockers.append(f"pool {pool.pool_id} references missing role {role_id}")
        if int(pool.max_count) < 1:
            blockers.append(f"pool {pool.pool_id} has no available agent")
        if not str(pool.backend).strip():
            blockers.append(f"pool {pool.pool_id} has no backend")
        if role_id != str(team_spec.bootstrap_supervisor) and not str(pool.model or "").strip():
            blockers.append(f"pool {pool.pool_id} has no model for role {role_id}")
    return blockers


def team_snapshot(team_spec: Any) -> dict[str, Any]:
    roles = []
    for role in team_spec.roles:
        if hasattr(role, "to_dict"):
            roles.append(role.to_dict())
        else:
            roles.append({"role_id": str(role.role_id), "version": int(role.version), "title": str(role.title), "required_capabilities": list(role.required_capabilities)})
    pools = []
    for pool in team_spec.agent_pools:
        pools.append({
            "pool_id": str(pool.pool_id),
            "backend": str(pool.backend),
            "role_id": str(pool.role_id),
            "count": int(pool.count),
            "max_count": int(pool.max_count),
            "model": pool.model,
            "provider_id": pool.provider_id,
            "execution_mode": str(pool.execution_mode),
        })
    return {
        "schema_version": int(team_spec.schema_version),
        "team_id": str(team_spec.team_id),
        "bootstrap_supervisor": str(team_spec.bootstrap_supervisor),
        "roles": roles,
        "agent_pools": pools,
    }


def snapshot_digest(snapshot: Mapping[str, Any]) -> str:
    canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_role_prompt(role: Any, *, task_package: Mapping[str, Any] | None = None, team_snapshot_data: Mapping[str, Any] | None = None) -> str:
    """Build a deterministic prompt; policy fields remain descriptive only."""
    sections = [f"ROLE: {role.title} ({role.role_id} v{role.version})", f"GOAL: {role.goal}"]
    for label, values in (
        ("RESPONSIBILITIES", role.responsibilities),
        ("FORBIDDEN", role.forbidden_actions),
        ("INPUT CONTRACT", role.input_contract),
        ("OUTPUT CONTRACT", role.output_contract),
        ("TOOLS", role.tool_policy),
        ("CONTEXT", role.context_policy),
    ):
        if values:
            sections.append(f"{label}:\n" + "\n".join(f"- {value}" for value in values))
    if team_snapshot_data is not None:
        sections.append("TEAM CAPABILITIES (descriptive; Hub policy is authoritative):\n" + json.dumps(team_snapshot_data, ensure_ascii=False, sort_keys=True))
    if task_package is not None:
        sections.append("APPROVED TASK PACKAGE:\n" + json.dumps(task_package, ensure_ascii=False, sort_keys=True))
    return "\n\n".join(sections)


def build_supervisor_plan_prompt(team_spec: Any, *, user_goal: str, budget: Mapping[str, Any] | None = None) -> str:
    snapshot = team_snapshot(team_spec)
    directory = [
        {"role_id": pool["role_id"], "backend": pool["backend"], "model": pool["model"], "provider_id": pool["provider_id"], "max_count": pool["max_count"]}
        for pool in snapshot["agent_pools"]
    ]
    contract = {
        "plan_version": 2,
        "revision": 1,
        "worker_concurrency": "integer <= available worker slots",
        "task_cap": "integer <= budget and may exceed worker_concurrency for sequential work",
        "tasks": "each task includes acceptance_criteria, input_refs, task_kind, output_contract",
    }
    return "\n\n".join((
        "You are the supervisor. Return JSON only; the Hub validates it before execution.",
        f"USER GOAL:\n{user_goal.strip()}",
        "ROLE/POOL DIRECTORY:\n" + json.dumps(directory, ensure_ascii=False, sort_keys=True),
        "BUDGET:\n" + json.dumps(dict(budget or {}), ensure_ascii=False, sort_keys=True),
        "LEGAL PLAN SHAPE:\n" + json.dumps(contract, ensure_ascii=False, sort_keys=True),
    ))
