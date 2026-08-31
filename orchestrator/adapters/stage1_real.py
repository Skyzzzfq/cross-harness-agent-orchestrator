from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from orchestrator.adapters.codebuddy_config import (
    codebuddy_china_environment,
    preferred_codebuddy_cli,
)
from orchestrator.adapters.codebuddy_safety_spike import path_is_within


def extract_cli_result(stdout: str) -> dict[str, Any] | None:
    """Return the single terminal result from either supported JSON shape."""
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(payload, dict):
        return payload if payload.get("type") == "result" else None
    if isinstance(payload, list):
        results = [
            item
            for item in payload
            if isinstance(item, dict) and item.get("type") == "result"
        ]
        return results[-1] if len(results) == 1 else None
    return None


def _cli_version(cli_path: str) -> str | None:
    package_json = (
        Path(cli_path).resolve().parent.parent
        / "@tencent-ai"
        / "codebuddy-code"
        / "package.json"
    )
    try:
        value = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    version = value.get("version") if isinstance(value, dict) else None
    return version if isinstance(version, str) else None


def _run_managed_process(
    command: list[str], *, cwd: Path, environment: dict[str, str], timeout: float
) -> tuple[subprocess.CompletedProcess[str] | None, bool]:
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        text=True,
        creationflags=creationflags,
        start_new_session=os.name != "nt",
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(
                ("taskkill", "/PID", str(process.pid), "/T", "/F"),
                capture_output=True,
                check=False,
                text=True,
            )
        else:
            os.killpg(process.pid, signal.SIGKILL)
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
        return None, process.poll() is not None
    return (
        subprocess.CompletedProcess(command, process.returncode, stdout, stderr),
        True,
    )


def run_codebuddy_write(
    cwd: Path,
    relative_path: str,
    content: str,
    *,
    session_id: str | None = None,
    resume: bool = False,
) -> dict[str, Any]:
    target = (cwd / relative_path).resolve()
    if not path_is_within(target, cwd) or any(
        part.casefold() == ".git" for part in Path(relative_path).parts
    ):
        return {
            "status": "blocked",
            "invoked_backend": False,
            "policy_decision": "blocked:outside-or-reserved-path",
        }
    cli_path = preferred_codebuddy_cli(cwd)
    if not cli_path:
        return {
            "status": "blocked",
            "invoked_backend": False,
            "policy_decision": "blocked:cli-unavailable",
        }
    requested_session = session_id or f"agent-hub-real-{uuid.uuid4().hex}"
    output_schema = {
        "type": "object",
        "properties": {"content": {"type": "string"}},
        "required": ["content"],
        "additionalProperties": False,
    }
    command = [
        cli_path,
        "-p",
        "--output-format",
        "json",
        "--permission-mode",
        "plan",
        "--tools",
        "StructuredOutput",
        "--json-schema",
        json.dumps(output_schema, separators=(",", ":")),
        "--setting-sources",
        "project",
        "--model",
        "glm-5.3",
    ]
    if resume:
        command.extend(("--resume", requested_session))
    else:
        command.extend(("--session-id", requested_session))
    command.append(
        (
            "Return the requested artifact as structured output without calling "
            f"tools. The content field must be exactly {content!r}."
        )
    )
    allowed_environment = {
        "ALLUSERSPROFILE",
        "APPDATA",
        "COMSPEC",
        "HOMEDRIVE",
        "HOMEPATH",
        "LOCALAPPDATA",
        "NODE_EXTRA_CA_CERTS",
        "NO_PROXY",
        "OS",
        "PATH",
        "PATHEXT",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERDOMAIN",
        "USERNAME",
        "USERPROFILE",
        "WINDIR",
    }
    environment = {
        **{
            name: value
            for name, value in os.environ.items()
            if name.upper() in allowed_environment
        },
        **codebuddy_china_environment(),
        "CODEBUDDY_DISABLE_AUTO_MEMORY": "1",
        "DISABLE_AUTOUPDATER": "1",
    }
    started_at = time.monotonic()
    completed, process_tree_stopped = _run_managed_process(
        command, cwd=cwd, environment=environment, timeout=90
    )
    if completed is None:
        return {
            "status": "timeout",
            "invoked_backend": True,
            "session_id": requested_session,
            "started_at": started_at,
            "ended_at": time.monotonic(),
            "process_tree_stopped": process_tree_stopped,
        }
    result_item = extract_cli_result(completed.stdout)
    structured = result_item.get("structured_output") if result_item else None
    generated = structured.get("content") if isinstance(structured, dict) else None
    if (
        completed.returncode == 0
        and result_item
        and result_item.get("subtype") == "success"
        and generated == content
    ):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f"{generated}\n".encode("utf-8"))
    expected_bytes = f"{content}\n".encode("utf-8")
    matched = target.is_file() and target.read_bytes() == expected_bytes
    succeeded = bool(
        completed.returncode == 0
        and result_item
        and result_item.get("subtype") == "success"
        and matched
    )
    return {
        "status": "completed" if succeeded else "failed",
        "invoked_backend": True,
        "policy_decision": "allowed:managed-worktree",
        "session_id": result_item.get("session_id") if result_item else requested_session,
        "duration_ms": result_item.get("duration_ms") if result_item else None,
        "started_at": started_at,
        "ended_at": time.monotonic(),
        "return_code": completed.returncode,
        "process_tree_stopped": process_tree_stopped,
        "content_matched": matched,
        "cli_sha256": hashlib.sha256(Path(cli_path).read_bytes()).hexdigest(),
        "cli_path": str(Path(cli_path).resolve()),
        "cli_version": _cli_version(cli_path),
        "usage": {
            key: result_item[key]
            for key in (
                "duration_api_ms",
                "num_turns",
                "total_cost_usd",
                "usage",
            )
            if result_item and key in result_item
        },
        "write_enforcement": "backend-tools-disabled;adapter-materialized-declared-path",
    }
