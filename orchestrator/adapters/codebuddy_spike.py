from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any

from orchestrator.adapters.codebuddy_config import (
    codebuddy_china_environment,
    preferred_codebuddy_cli,
)
from orchestrator.adapters.contracts import ProbeStatus


def assess_session_isolation(
    workers: list[dict[str, Any]], resume: dict[str, Any]
) -> dict[str, Any]:
    successful_workers = [item for item in workers if item.get("matched")]
    session_ids = {item.get("session_id") for item in successful_workers}
    overlap = False
    if len(successful_workers) == 2:
        overlap = max(item["started_at"] for item in successful_workers) < min(
            item["ended_at"] for item in successful_workers
        )

    checks = {
        "two_workers_succeeded": len(successful_workers) == 2,
        "session_ids_are_distinct": len(session_ids) == 2 and None not in session_ids,
        "execution_overlapped": overlap,
        "working_directories_are_distinct": len(
            {item.get("cwd") for item in successful_workers}
        )
        == 2,
        "contexts_are_isolated": {item.get("marker") for item in successful_workers}
        == {"CODEBUDDY_AGENT_A_OK", "CODEBUDDY_AGENT_B_OK"},
        "session_resume_succeeded": bool(resume.get("matched")),
    }
    ready = all(checks.values())
    return {
        "backend": "codebuddy",
        "status": (
            ProbeStatus.READY if ready else ProbeStatus.ERROR
        ).value,
        "checks": checks,
        "workers": workers,
        "resume": resume,
    }


async def _read_marker(cwd: Path, marker: str, session_id: str) -> dict[str, Any]:
    from codebuddy_agent_sdk import (
        AssistantMessage,
        CodeBuddyAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )

    options = CodeBuddyAgentOptions(
        session_id=session_id,
        cwd=cwd,
        codebuddy_code_path=preferred_codebuddy_cli(cwd),
        max_turns=2,
        permission_mode="plan",
        tools=["Read"],
        allowed_tools=["Read"],
        disallowed_tools=["Bash", "PowerShell", "Write", "Edit"],
        request_timeout_ms=60_000,
        setting_sources=[],
        env=codebuddy_china_environment(),
    )
    started_at = time.monotonic()
    text_parts: list[str] = []
    result: ResultMessage | None = None
    try:
        async for message in query(
            prompt=(
                "Read marker.txt from the current working directory and reply with "
                "exactly its contents. Do not add punctuation or explanation."
            ),
            options=options,
        ):
            if isinstance(message, AssistantMessage):
                text_parts.extend(
                    block.text
                    for block in message.content
                    if isinstance(block, TextBlock)
                )
            elif isinstance(message, ResultMessage):
                result = message
    except Exception as exc:
        return {
            "marker": marker,
            "cwd": str(cwd),
            "session_id": session_id,
            "started_at": started_at,
            "ended_at": time.monotonic(),
            "matched": False,
            "error_type": type(exc).__name__,
        }

    response = "".join(text_parts).strip()
    return {
        "marker": marker,
        "cwd": str(cwd),
        "session_id": result.session_id if result else session_id,
        "started_at": started_at,
        "ended_at": time.monotonic(),
        "duration_ms": result.duration_ms if result else None,
        "matched": bool(result and not result.is_error and response == marker),
    }


async def _resume_marker(cwd: Path, marker: str, session_id: str) -> dict[str, Any]:
    from codebuddy_agent_sdk import (
        AssistantMessage,
        CodeBuddyAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )

    options = CodeBuddyAgentOptions(
        resume=session_id,
        cwd=cwd,
        codebuddy_code_path=preferred_codebuddy_cli(cwd),
        max_turns=1,
        permission_mode="plan",
        tools=[],
        request_timeout_ms=60_000,
        setting_sources=[],
        env=codebuddy_china_environment(),
    )
    text_parts: list[str] = []
    result: ResultMessage | None = None
    try:
        async for message in query(
            prompt=(
                "Without using tools, reply with exactly the marker you read in the "
                "previous turn. Do not add punctuation or explanation."
            ),
            options=options,
        ):
            if isinstance(message, AssistantMessage):
                text_parts.extend(
                    block.text
                    for block in message.content
                    if isinstance(block, TextBlock)
                )
            elif isinstance(message, ResultMessage):
                result = message
    except Exception as exc:
        return {"matched": False, "error_type": type(exc).__name__}

    return {
        "session_id": result.session_id if result else session_id,
        "duration_ms": result.duration_ms if result else None,
        "matched": bool(
            result
            and not result.is_error
            and "".join(text_parts).strip() == marker
        ),
    }


async def _run_codebuddy_session_spike(cwd: Path) -> dict[str, Any]:
    spike_root = cwd / ".agent-hub" / "spike" / "codebuddy-sessions"
    worker_specs = [
        (spike_root / "worker-a", "CODEBUDDY_AGENT_A_OK"),
        (spike_root / "worker-b", "CODEBUDDY_AGENT_B_OK"),
    ]
    for worker_dir, marker in worker_specs:
        worker_dir.mkdir(parents=True, exist_ok=True)
        (worker_dir / "marker.txt").write_text(marker, encoding="utf-8")

    session_ids = [f"agent-hub-spike-{uuid.uuid4().hex}" for _ in worker_specs]
    workers = await asyncio.gather(
        *(
            _read_marker(worker_dir, marker, session_id)
            for (worker_dir, marker), session_id in zip(
                worker_specs, session_ids, strict=True
            )
        )
    )
    first = workers[0]
    resume = (
        await _resume_marker(
            worker_specs[0][0], worker_specs[0][1], str(first["session_id"])
        )
        if first.get("matched")
        else {"matched": False, "skipped": True}
    )
    return assess_session_isolation(workers, resume)


def run_codebuddy_session_spike(cwd: Path) -> dict[str, Any]:
    return asyncio.run(_run_codebuddy_session_spike(cwd))
