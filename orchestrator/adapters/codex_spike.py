from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

from orchestrator.adapters.contracts import ProbeStatus


def assess_codex_lifecycle(checks: dict[str, bool]) -> str:
    return (
        ProbeStatus.READY if checks and all(checks.values()) else ProbeStatus.ERROR
    ).value


async def _run_codex_lifecycle_spike(cwd: Path) -> dict[str, Any]:
    from openai_codex import (
        ApprovalMode,
        AsyncCodex,
        CodexConfig,
        Sandbox,
    )

    from orchestrator.platform import codex_transport_environment

    spike_cwd = cwd / ".agent-hub" / "spike" / "codex-lifecycle"
    spike_cwd.mkdir(parents=True, exist_ok=True)
    marker = f"CODEX_RESUME_{uuid.uuid4().hex[:12]}"
    thread_id: str | None = None
    initial: dict[str, Any] = {"matched": False}
    resume: dict[str, Any] = {"matched": False}
    failure: dict[str, Any] = {"recognized": False}
    control: dict[str, Any] = {
        "timeout_recognized": False,
        "cancel_recognized": False,
    }

    async with AsyncCodex(
        CodexConfig(env=codex_transport_environment(cwd))
    ) as codex:
        try:
            thread = await codex.thread_start(
                approval_mode=ApprovalMode.deny_all,
                cwd=str(spike_cwd),
                ephemeral=False,
                sandbox=Sandbox.read_only,
            )
            thread_id = thread.id
            first = await asyncio.wait_for(
                thread.run(
                    (
                        f"Remember this marker for the next turn: {marker}. Reply "
                        "with exactly that marker. Do not call tools or modify files."
                    )
                ),
                timeout=60.0,
            )
            initial = {
                "thread_id": thread.id,
                "turn_id": first.id,
                "status": first.status.value,
                "duration_ms": first.duration_ms,
                "matched": first.final_response.strip() == marker
                if first.final_response
                else False,
            }

            resumed = await codex.thread_resume(
                thread.id,
                approval_mode=ApprovalMode.deny_all,
                cwd=str(spike_cwd),
                sandbox=Sandbox.read_only,
            )
            second = await asyncio.wait_for(
                resumed.run(
                    (
                        "Reply with exactly the marker from the previous turn. Do "
                        "not call tools or modify files."
                    )
                ),
                timeout=60.0,
            )
            resume = {
                "thread_id": resumed.id,
                "turn_id": second.id,
                "status": second.status.value,
                "duration_ms": second.duration_ms,
                "same_thread": resumed.id == thread.id,
                "matched": second.final_response.strip() == marker
                if second.final_response
                else False,
            }

            try:
                await asyncio.wait_for(
                    codex.thread_resume(
                        "00000000-0000-0000-0000-000000000000",
                        approval_mode=ApprovalMode.deny_all,
                        cwd=str(spike_cwd),
                        sandbox=Sandbox.read_only,
                    ),
                    timeout=15.0,
                )
            except Exception as exc:
                failure = {
                    "recognized": True,
                    "error_type": type(exc).__name__,
                }

            turn = await resumed.turn(
                (
                    "Use the terminal tool to run exactly this read-only command: "
                    "powershell.exe -NoProfile -Command Start-Sleep -Seconds 30. "
                    "After it finishes, reply with DONE."
                )
            )
            turn_result = asyncio.create_task(turn.run())
            try:
                await asyncio.wait_for(asyncio.shield(turn_result), timeout=0.5)
            except TimeoutError:
                control["timeout_recognized"] = True
                try:
                    await turn.interrupt()
                    interrupted = await asyncio.wait_for(turn_result, timeout=30.0)
                    control.update(
                        {
                            "turn_id": turn.id,
                            "final_status": interrupted.status.value,
                            "cancel_recognized": interrupted.status.value
                            == "interrupted",
                        }
                    )
                except Exception as exc:
                    control["interrupt_error_type"] = type(exc).__name__
                    if turn_result.done() and not turn_result.cancelled():
                        completed = turn_result.result()
                        control["final_status"] = completed.status.value
        finally:
            if thread_id:
                await asyncio.wait_for(codex.thread_archive(thread_id), timeout=15.0)

    checks = {
        "success_recognized": bool(initial.get("matched")),
        "failure_recognized": bool(failure.get("recognized")),
        "timeout_recognized": bool(control.get("timeout_recognized")),
        "cancel_recognized": bool(control.get("cancel_recognized")),
        "session_resume_succeeded": bool(
            resume.get("matched") and resume.get("same_thread")
        ),
        "read_only_cwd_was_selected": initial.get("status") == "completed",
    }
    return {
        "backend": "codex",
        "status": assess_codex_lifecycle(checks),
        "checks": checks,
        "initial": initial,
        "resume": resume,
        "failure": failure,
        "control": control,
        "cleanup": {"thread_archived": thread_id is not None},
    }


def run_codex_lifecycle_spike(cwd: Path) -> dict[str, Any]:
    return asyncio.run(_run_codex_lifecycle_spike(cwd))
