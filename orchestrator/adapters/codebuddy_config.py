from __future__ import annotations

import os
from pathlib import Path


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
