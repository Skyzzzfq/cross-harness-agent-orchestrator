from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from orchestrator.adapters.codebuddy_config import (
    CODEBUDDY_REGION,
    codebuddy_china_environment,
    preferred_codebuddy_cli,
)
from orchestrator.adapters.probes import codex_executable
from orchestrator.platform import codex_transport_environment


def login_codex(cwd: Path) -> int:
    executable = codex_executable()
    if not executable:
        print("Codex CLI is unavailable. Install the Codex SDK first.")
        return 2
    completed = subprocess.run(
        [executable, "login", "--device-auth"],
        check=False,
        env=codex_transport_environment(cwd),
    )
    return completed.returncode


async def _login_codebuddy(cwd: Path) -> int:
    from codebuddy_agent_sdk import authenticate

    auth = await authenticate(
        environment=CODEBUDDY_REGION,
        env=codebuddy_china_environment(),
        codebuddy_code_path=preferred_codebuddy_cli(cwd),
        timeout=300.0,
    )
    if auth.auth_url:
        print("Open this one-time CodeBuddy sign-in URL in your browser:")
        print(auth.auth_url)
    await auth
    print("CodeBuddy sign-in completed.")
    return 0


def login_codebuddy(cwd: Path) -> int:
    return asyncio.run(_login_codebuddy(cwd))


def login(backend: str, cwd: Path) -> int:
    if backend == "codex":
        return login_codex(cwd)
    if backend == "codebuddy":
        return login_codebuddy(cwd)
    raise ValueError(f"Unsupported backend: {backend}")
