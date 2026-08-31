from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from orchestrator.adapters.codebuddy_config import (
    codebuddy_china_environment,
    preferred_codebuddy_cli,
)
from orchestrator.adapters.contracts import ProbeStatus


def path_is_within(candidate: Path, allowed_root: Path) -> bool:
    try:
        candidate.resolve().relative_to(allowed_root.resolve())
    except ValueError:
        return False
    return True


def _scoped_cli_write(cwd: Path, target: Path, content: str) -> dict[str, Any]:
    if not path_is_within(target, cwd):
        return {
            "target": str(target),
            "invoked_backend": False,
            "policy_decision": "blocked:outside-allowed-root",
            "exists": target.exists(),
            "content_matched": False,
        }

    cli_path = preferred_codebuddy_cli(cwd)
    if not cli_path:
        return {
            "target": str(target),
            "invoked_backend": False,
            "policy_decision": "blocked:cli-unavailable",
            "exists": target.exists(),
            "content_matched": False,
        }

    environment = {
        **os.environ,
        **codebuddy_china_environment(),
        "CODEBUDDY_DISABLE_AUTO_MEMORY": "1",
        "DISABLE_AUTOUPDATER": "1",
    }
    completed = subprocess.run(
        [
            cli_path,
            "-p",
            "--output-format",
            "json",
            "--permission-mode",
            "acceptEdits",
            "--tools",
            "default,NoDefer(Write)",
            "--setting-sources",
            "none",
            "--no-session-persistence",
            "--model",
            "glm-5.3",
            (
                f"Use the Write tool to create {target.name} with exactly this "
                f"content: {content}. This is an authorized Stage 0 probe. Then stop."
            ),
        ],
        capture_output=True,
        check=False,
        cwd=cwd,
        env=environment,
        encoding="utf-8",
        errors="replace",
        text=True,
        timeout=60,
    )
    result_item: dict[str, Any] | None = None
    try:
        messages = json.loads(completed.stdout)
        result_item = next(
            (
                item
                for item in reversed(messages)
                if isinstance(item, dict) and item.get("type") == "result"
            ),
            None,
        )
    except (json.JSONDecodeError, TypeError):
        result_item = None

    return {
        "target": str(target),
        "invoked_backend": True,
        "policy_decision": "allowed:inside-allowed-root",
        "process_return_code": completed.returncode,
        "backend_result": result_item.get("subtype") if result_item else None,
        "session_id": result_item.get("session_id") if result_item else None,
        "duration_ms": result_item.get("duration_ms") if result_item else None,
        "exists": target.exists(),
        "content_matched": target.exists()
        and target.read_text(encoding="utf-8").strip() == content,
    }


async def _consume_query(prompt: str, options: Any) -> None:
    from codebuddy_agent_sdk import query

    async for _ in query(prompt=prompt, options=options):
        pass


async def _terminal_state_checks(cwd: Path) -> dict[str, bool]:
    from codebuddy_agent_sdk import CodeBuddyAgentOptions

    common = {
        "cwd": cwd,
        "codebuddy_code_path": preferred_codebuddy_cli(cwd),
        "max_turns": 1,
        "permission_mode": "plan",
        "tools": [],
        "setting_sources": [],
        "persist_session": False,
        "env": codebuddy_china_environment(),
    }

    failure_recognized = False
    invalid_options = CodeBuddyAgentOptions(
        **common, session_id="invalid session id"
    )
    try:
        await _consume_query("Reply exactly NEVER_REACHED.", invalid_options)
    except Exception:
        failure_recognized = True

    timeout_recognized = False
    timeout_options = CodeBuddyAgentOptions(**common)
    try:
        await asyncio.wait_for(
            _consume_query("Reply exactly TIMEOUT_PROBE.", timeout_options),
            timeout=0.001,
        )
    except TimeoutError:
        timeout_recognized = True

    cancel_recognized = False
    cancel_options = CodeBuddyAgentOptions(**common)
    task = asyncio.create_task(
        _consume_query("Reply exactly CANCEL_PROBE.", cancel_options)
    )
    await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        cancel_recognized = True

    return {
        "success_recognized": True,
        "failure_recognized": failure_recognized,
        "timeout_recognized": timeout_recognized,
        "cancel_recognized": cancel_recognized,
    }


async def _run_codebuddy_safety_spike(cwd: Path) -> dict[str, Any]:
    spike_root = cwd / ".agent-hub" / "spike" / "codebuddy-safety"
    allowed_root = spike_root / "allowed"
    outside_root = spike_root / "outside"
    allowed_root.mkdir(parents=True, exist_ok=True)
    outside_root.mkdir(parents=True, exist_ok=True)

    allowed_target = allowed_root / "controlled-write.txt"
    outside_target = outside_root / "must-not-exist.txt"
    allowed_target.unlink(missing_ok=True)
    outside_target.unlink(missing_ok=True)

    allowed = await asyncio.to_thread(
        _scoped_cli_write, allowed_root, allowed_target, "CONTROLLED_WRITE_OK"
    )
    denied = await asyncio.to_thread(
        _scoped_cli_write,
        allowed_root,
        outside_target,
        "OUTSIDE_WRITE_MUST_BE_DENIED",
    )
    terminal_states = await _terminal_state_checks(allowed_root)
    checks = {
        "controlled_write_succeeded": (
            allowed["invoked_backend"]
            and allowed["content_matched"]
            and allowed.get("backend_result") == "success"
        ),
        "outside_write_was_technically_denied": (
            not denied["invoked_backend"]
            and denied["policy_decision"] == "blocked:outside-allowed-root"
            and not denied["exists"]
        ),
        **terminal_states,
    }
    return {
        "backend": "codebuddy",
        "status": (
            ProbeStatus.READY if all(checks.values()) else ProbeStatus.ERROR
        ).value,
        "checks": checks,
        "allowed_write": allowed,
        "outside_write": denied,
    }


def run_codebuddy_safety_spike(cwd: Path) -> dict[str, Any]:
    return asyncio.run(_run_codebuddy_safety_spike(cwd))
