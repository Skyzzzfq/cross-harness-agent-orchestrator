from __future__ import annotations

from pathlib import Path
from typing import Any

from orchestrator.core.config import load_team_spec
from orchestrator.storage.sqlite_store import SQLiteStateStore


DEFAULT_TEAM_PATH = Path("config/team.yaml")
DEFAULT_DATABASE_PATH = Path(".agent-hub/state/agent-hub.db")


def _resolve(cwd: Path, path: Path) -> Path:
    return path if path.is_absolute() else cwd / path


def initialize_hub(
    cwd: Path,
    *,
    team_path: Path = DEFAULT_TEAM_PATH,
    database_path: Path = DEFAULT_DATABASE_PATH,
) -> dict[str, Any]:
    resolved_team = _resolve(cwd, team_path)
    resolved_database = _resolve(cwd, database_path)
    team = load_team_spec(resolved_team)
    with SQLiteStateStore(resolved_database) as store:
        summary = store.summary()
    return {
        "status": "ready",
        "team_id": team.team_id,
        "team_path": str(resolved_team),
        "database_path": str(resolved_database),
        "agent_pools": [
            {
                "pool_id": pool.pool_id,
                "backend": pool.backend,
                "role_id": pool.role_id,
                "count": pool.count,
                "max_count": pool.max_count,
            }
            for pool in team.agent_pools
        ],
        "summary": summary,
    }


def hub_status(
    cwd: Path,
    *,
    database_path: Path = DEFAULT_DATABASE_PATH,
    run_id: str | None = None,
) -> dict[str, Any]:
    resolved_database = _resolve(cwd, database_path)
    if not resolved_database.is_file():
        return {
            "status": "uninitialized",
            "database_path": str(resolved_database),
        }
    with SQLiteStateStore(resolved_database) as store:
        summary = store.summary(run_id=run_id)
    return {
        "status": "ready" if run_id is None or summary["runs"] == 1 else "not-found",
        "database_path": str(resolved_database),
        "run_id": run_id,
        "summary": summary,
    }
