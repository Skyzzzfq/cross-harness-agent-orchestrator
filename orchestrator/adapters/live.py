from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from orchestrator.adapters.codebuddy_config import (
    CODEBUDDY_REGION,
    codebuddy_china_environment,
    preferred_codebuddy_cli,
)
from orchestrator.adapters.contracts import ProbeStatus


_CODEX_PROMPT = (
    "Reply with exactly CODEX_PROBE_OK. Do not call tools and do not modify files."
)
_CODEBUDDY_PROMPT = (
    "Reply with exactly CODEBUDDY_PROBE_OK. Do not call tools and do not modify files."
)


def run_codex_live(cwd: Path) -> dict[str, Any]:
    from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox

    from orchestrator.platform import codex_transport_environment

    try:
        with Codex(CodexConfig(env=codex_transport_environment(cwd))) as codex:
            thread = codex.thread_start(
                approval_mode=ApprovalMode.deny_all,
                cwd=str(cwd),
                ephemeral=True,
                sandbox=Sandbox.read_only,
            )
            result = thread.run(_CODEX_PROMPT)
    except Exception as exc:  # SDK exceptions are mapped at the adapter boundary.
        return {
            "backend": "codex",
            "status": ProbeStatus.ERROR.value,
            "auth": "failed-or-unavailable",
            "error_type": type(exc).__name__,
            "message": str(exc),
        }

    response = (result.final_response or "").strip()
    succeeded = response == "CODEX_PROBE_OK"
    return {
        "backend": "codex",
        "status": (ProbeStatus.READY if succeeded else ProbeStatus.ERROR).value,
        "auth": "verified",
        "thread_id": thread.id,
        "turn_status": str(result.status),
        "duration_ms": result.duration_ms,
        "response_matched": succeeded,
    }


async def _run_codebuddy_live(cwd: Path) -> dict[str, Any]:
    from codebuddy_agent_sdk import (
        AssistantMessage,
        CodeBuddyAgentOptions,
        ResultMessage,
        TextBlock,
        authenticate,
        query,
    )

    try:
        auth = await authenticate(
            environment=CODEBUDDY_REGION,
            env=codebuddy_china_environment(),
            codebuddy_code_path=preferred_codebuddy_cli(cwd),
            timeout=15.0,
        )
        if auth.auth_url:
            await auth.cancel()
            return {
                "backend": "codebuddy",
                "status": ProbeStatus.BLOCKED.value,
                "auth": "interactive-login-required",
            }
        await auth

        options = CodeBuddyAgentOptions(
            cwd=cwd,
            codebuddy_code_path=preferred_codebuddy_cli(cwd),
            max_turns=1,
            permission_mode="plan",
            request_timeout_ms=60_000,
            setting_sources=[],
            env=codebuddy_china_environment(),
        )
        text_parts: list[str] = []
        result_message: ResultMessage | None = None
        async for message in query(prompt=_CODEBUDDY_PROMPT, options=options):
            if isinstance(message, AssistantMessage):
                text_parts.extend(
                    block.text
                    for block in message.content
                    if isinstance(block, TextBlock)
                )
            elif isinstance(message, ResultMessage):
                result_message = message
    except Exception as exc:  # SDK exceptions are mapped at the adapter boundary.
        return {
            "backend": "codebuddy",
            "status": ProbeStatus.ERROR.value,
            "auth": "failed-or-unavailable",
            "error_type": type(exc).__name__,
            "message": str(exc),
        }

    response = "".join(text_parts).strip()
    succeeded = (
        result_message is not None
        and not result_message.is_error
        and response == "CODEBUDDY_PROBE_OK"
    )
    return {
        "backend": "codebuddy",
        "status": (ProbeStatus.READY if succeeded else ProbeStatus.ERROR).value,
        "auth": "verified",
        "session_id": result_message.session_id if result_message else None,
        "duration_ms": result_message.duration_ms if result_message else None,
        "response_matched": succeeded,
    }


def run_codebuddy_live(cwd: Path) -> dict[str, Any]:
    return asyncio.run(_run_codebuddy_live(cwd))


def run_live(backend: str, cwd: Path) -> dict[str, Any]:
    if backend == "codex":
        return run_codex_live(cwd)
    if backend == "codebuddy":
        return run_codebuddy_live(cwd)
    raise ValueError(f"Unsupported backend: {backend}")
