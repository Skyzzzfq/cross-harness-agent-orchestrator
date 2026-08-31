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


@dataclass(frozen=True)
class AgentPoolSpec:
    pool_id: str
    backend: str
    role_id: str
    count: int
    max_count: int
    model: str | None = None
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
    roles = tuple(
        RoleSpec(
            role_id=str(item["role_id"]),
            version=int(item["version"]),
            title=str(item["title"]),
            required_capabilities=tuple(item.get("required_capabilities", ())),
        )
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
