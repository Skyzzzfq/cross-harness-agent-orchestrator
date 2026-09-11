from __future__ import annotations

import os
from pathlib import Path

from orchestrator.console.settings import load_model_providers


CODEBUDDY_REGION = "internal"


def codebuddy_china_environment() -> dict[str, str]:
    """Return a fresh environment mapping for the CodeBuddy China service."""
    return {"CODEBUDDY_INTERNET_ENVIRONMENT": CODEBUDDY_REGION}


def preferred_codebuddy_cli(start: Path) -> str | None:
    configured = os.environ.get("AGENT_HUB_CODEBUDDY_BIN") or os.environ.get(
        "CODEBUDDY_CODE_PATH"
    )
    if configured and Path(configured).is_file():
        return configured

    resolved = start.resolve()
    for root in (resolved, *resolved.parents):
        candidate = (
            root
            / ".agent-hub"
            / "tools"
            / "node_modules"
            / ".bin"
            / "codebuddy.cmd"
        )
        if candidate.is_file():
            return str(candidate)
    return None


def codebuddy_environment_for_provider(
    cwd: Path, provider_id: str | None
) -> dict[str, str]:
    """Build the CodeBuddy child environment for the selected provider."""
    if not provider_id:
        return codebuddy_china_environment()
    project_root = cwd.resolve()
    for candidate in (project_root, *project_root.parents):
        if (candidate / ".agent-hub").is_dir():
            project_root = candidate
            break
    provider = next(
        (
            item
            for item in load_model_providers(project_root)
            if item["provider_id"] == provider_id
        ),
        None,
    )
    if provider is None:
        raise ValueError(f"model provider is not configured: {provider_id}")
    api_key_env = str(provider["api_key_env"])
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise ValueError(
            f"model provider key environment variable is not set: {api_key_env}"
        )
    return {
        "CODEBUDDY_BASE_URL": str(provider["base_url"]),
        "CODEBUDDY_API_KEY": api_key,
    }
