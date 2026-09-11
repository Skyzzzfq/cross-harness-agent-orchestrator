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


async def _login_codebuddy(cwd: Path, *, open_browser: bool = False) -> int:
    from codebuddy_agent_sdk import authenticate

    auth = await authenticate(
        environment=CODEBUDDY_REGION,
        env=codebuddy_china_environment(),
        codebuddy_code_path=preferred_codebuddy_cli(cwd),
        timeout=300.0,
    )
    if auth.auth_url:
        if open_browser:
            import webbrowser

            if not webbrowser.open_new(auth.auth_url):
                print("Unable to open the browser automatically.")
                print("Open this one-time CodeBuddy sign-in URL manually:")
                print(auth.auth_url)
        else:
            print("Open this one-time CodeBuddy sign-in URL in your browser:")
            print(auth.auth_url)
    await auth
    print("CodeBuddy sign-in completed.")
    return 0


def login_codebuddy(cwd: Path, *, open_browser: bool = False) -> int:
    return asyncio.run(_login_codebuddy(cwd, open_browser=open_browser))


def login(backend: str, cwd: Path, *, open_browser: bool = False) -> int:
    if backend == "codex":
        return login_codex(cwd)
    if backend == "codebuddy":
        return login_codebuddy(cwd, open_browser=open_browser)
    raise ValueError(f"Unsupported backend: {backend}")
