from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from importlib.metadata import PackageNotFoundError, version

from orchestrator.adapters.contracts import ProbeResult, ProbeStatus


def _distribution_version(distribution: str) -> str | None:
    try:
        return version(distribution)
    except PackageNotFoundError:
        return None


def _module_available(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def _first_executable(environment_name: str, commands: tuple[str, ...]) -> str | None:
    configured = os.environ.get(environment_name)
    if configured:
        return configured
    for command in commands:
        candidate = shutil.which(command)
        if candidate:
            return candidate
    return None


def codex_executable() -> str | None:
    executable = _first_executable("AGENT_HUB_CODEX_BIN", ("codex",))
    if executable:
        return executable
    try:
        from codex_cli_bin import bundled_codex_path

        return str(bundled_codex_path())
    except (ImportError, OSError):
        return None


def _codex_saved_login(executable: str | None) -> bool:
    if not executable:
        return False
    try:
        completed = subprocess.run(
            [executable, "login", "status"],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    output = f"{completed.stdout}\n{completed.stderr}".lower()
    return completed.returncode == 0 and "not logged in" not in output


def probe_codex() -> ProbeResult:
    sdk_version = _distribution_version("openai-codex")
    sdk_importable = _module_available("openai_codex")
    executable = codex_executable()
    saved_auth = _codex_saved_login(executable)

    if not sdk_importable and not executable:
        status = ProbeStatus.UNAVAILABLE
    elif not saved_auth:
        status = ProbeStatus.BLOCKED
    else:
        status = ProbeStatus.READY

    notes: list[str] = []
    if not saved_auth:
        notes.append("Run Codex once and choose Sign in with ChatGPT.")
    if sdk_importable:
        notes.append("Using the Codex SDK pinned runtime.")

    return ProbeResult(
        backend="codex",
        status=status,
        version=sdk_version,
        entrypoint="python-sdk" if sdk_importable else executable,
        auth="saved-login-present" if saved_auth else "saved-login-missing",
        checks={
            "sdk_importable": sdk_importable,
            "cli_available": executable is not None,
            "saved_login_verified": saved_auth,
        },
        notes=tuple(notes),
    )


def probe_codebuddy() -> ProbeResult:
    sdk_version = _distribution_version("codebuddy-agent-sdk")
    sdk_importable = _module_available("codebuddy_agent_sdk")
    executable = _first_executable(
        "CODEBUDDY_CODE_PATH", ("codebuddy", "cbc")
    )

    status = ProbeStatus.READY if sdk_importable or executable else ProbeStatus.UNAVAILABLE
    notes: list[str] = []
    if sdk_importable and not executable:
        notes.append("Using the CodeBuddy SDK bundled executable; no PATH CLI was found.")
    notes.append("Authentication is confirmed only by the optional live probe.")

    return ProbeResult(
        backend="codebuddy",
        status=status,
        version=sdk_version,
        entrypoint="python-sdk" if sdk_importable else executable,
        auth="unverified",
        checks={
            "sdk_importable": sdk_importable,
            "cli_on_path": executable is not None,
        },
        notes=tuple(notes),
    )


def probe_all() -> list[ProbeResult]:
    return [probe_codex(), probe_codebuddy()]
