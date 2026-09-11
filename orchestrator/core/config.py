from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RoleSpec:
    role_id: str
    version: int
    title: str
    required_capabilities: tuple[str, ...]
    goal: str = ""
    responsibilities: tuple[str, ...] = ()
    forbidden_actions: tuple[str, ...] = ()
    input_contract: tuple[str, ...] = ()
    output_contract: tuple[str, ...] = ()
    tool_policy: tuple[str, ...] = ()
    context_policy: tuple[str, ...] = ()
    budget_defaults: tuple[tuple[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "role_id": self.role_id,
            "version": self.version,
            "title": self.title,
            "required_capabilities": list(self.required_capabilities),
            "goal": self.goal,
            "responsibilities": list(self.responsibilities),
            "forbidden_actions": list(self.forbidden_actions),
            "input_contract": list(self.input_contract),
            "output_contract": list(self.output_contract),
            "tool_policy": list(self.tool_policy),
            "context_policy": list(self.context_policy),
            "budget_defaults": {key: value for key, value in self.budget_defaults},
        }


@dataclass(frozen=True)
class AgentPoolSpec:
    pool_id: str
    backend: str
    role_id: str
    count: int
    max_count: int
    model: str | None = None
    provider_id: str | None = None
    execution_mode: str = "sdk_session"


@dataclass(frozen=True)
class TeamSpec:
    schema_version: int
    team_id: str
    bootstrap_supervisor: str
    roles: tuple[RoleSpec, ...]
    agent_pools: tuple[AgentPoolSpec, ...]


def _read_json_compatible_yaml(path: Path) -> dict[str, Any]:
    """Read the JSON-compatible subset of YAML 1.2 using the standard library."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"configuration must be an object: {path}")
    return data


def load_team_spec(path: Path) -> TeamSpec:
    data = _read_json_compatible_yaml(path)
    from orchestrator.core.role_registry import role_template_for

    roles = tuple(
        _load_role(item, role_template_for(str(item["role_id"])))
        for item in data["roles"]
    )
    pools = tuple(
        AgentPoolSpec(
            pool_id=str(item["pool_id"]),
            backend=str(item["backend"]),
            role_id=str(item["role_id"]),
            count=int(item["count"]),
            max_count=int(item["max_count"]),
            model=None if item.get("model") is None else str(item["model"]),
            provider_id=(
                None if item.get("provider_id") is None else str(item["provider_id"])
            ),
            execution_mode=str(item.get("execution_mode", "sdk_session")),
        )
        for item in data["agent_pools"]
    )
    spec = TeamSpec(
        schema_version=int(data["schema_version"]),
        team_id=str(data["team_id"]),
        bootstrap_supervisor=str(data["bootstrap_supervisor"]),
        roles=roles,
        agent_pools=pools,
    )
    _validate_team_spec(spec)
    return spec


def _load_role(item: dict[str, Any], defaults: dict[str, Any] | None) -> RoleSpec:
    """Load a role while keeping schema-v1 team files source compatible."""
    template = defaults or {}

    def values(name: str) -> tuple[str, ...]:
        raw = item.get(name, template.get(name, ()))
        if isinstance(raw, str):
            return (raw,)
        return tuple(str(value) for value in (raw or ()))

    raw_budget = item.get("budget_defaults", template.get("budget_defaults", {}))
    if not isinstance(raw_budget, dict):
        raise ValueError("budget_defaults must be an object")
    return RoleSpec(
        role_id=str(item["role_id"]),
        version=int(item.get("version", template.get("version", 1))),
        title=str(item.get("title", template.get("title", item["role_id"]))),
        required_capabilities=values("required_capabilities"),
        goal=str(item.get("goal", template.get("goal", ""))),
        responsibilities=values("responsibilities"),
        forbidden_actions=values("forbidden_actions"),
        input_contract=values("input_contract"),
        output_contract=values("output_contract"),
        tool_policy=values("tool_policy"),
        context_policy=values("context_policy"),
        budget_defaults=tuple(sorted((str(key), value) for key, value in raw_budget.items())),
    )


def _validate_team_spec(spec: TeamSpec) -> None:
    if spec.schema_version != 1:
        raise ValueError("unsupported team schema_version")
    role_ids = {role.role_id for role in spec.roles}
    if len(role_ids) != len(spec.roles):
        raise ValueError("role_id values must be unique")
    if spec.bootstrap_supervisor not in role_ids:
        raise ValueError("bootstrap_supervisor must reference a declared role")
    pool_ids = {pool.pool_id for pool in spec.agent_pools}
    if len(pool_ids) != len(spec.agent_pools):
        raise ValueError("pool_id values must be unique")
    for pool in spec.agent_pools:
        if pool.role_id not in role_ids:
            raise ValueError(f"pool references unknown role: {pool.role_id}")
        if pool.count < 0 or pool.count > pool.max_count:
            raise ValueError(f"invalid count for pool: {pool.pool_id}")
