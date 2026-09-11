"""Strict, side-effect-free validation for supervisor-generated task plans.

The supervisor proposes work only.  This module deliberately does not create
tasks, resolve cwd values, or grant permissions; those decisions stay with
the Hub and the existing workspace/controller boundaries.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from orchestrator.core.sanitize import redact_sensitive
from orchestrator.workspace.policy import validate_write_scope_static

PLAN_VERSION = 1
_PLAN_FIELDS = frozenset({"plan_version", "summary", "tasks"})
_TASK_FIELDS = frozenset(
    {
        "task_id",
        "role_id",
        "backend",
        "prompt",
        "access_mode",
        "write_scope",
        "depends_on",
    }
)


class PlanValidationError(ValueError):
    """Raised when an untrusted supervisor result is not a valid plan."""


@dataclass(frozen=True)
class SupervisorTaskSpec:
    task_id: str
    role_id: str
    backend: str
    prompt: str
    access_mode: str
    write_scope: tuple[str, ...]
    depends_on: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "role_id": self.role_id,
            "backend": self.backend,
            "prompt": self.prompt,
            "access_mode": self.access_mode,
            "write_scope": list(self.write_scope),
            "depends_on": list(self.depends_on),
        }


@dataclass(frozen=True)
class SupervisorPlan:
    plan_version: int
    summary: str
    tasks: tuple[SupervisorTaskSpec, ...]
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_version": self.plan_version,
            "summary": self.summary,
            "tasks": [task.to_dict() for task in self.tasks],
        }


def validate_supervisor_plan(
    payload: Any,
    *,
    allowed_role_ids: Collection[str],
    allowed_backends: Collection[str],
    max_tasks: int,
    existing_task_ids: Collection[str] = (),
    allowed_child_role_ids: Collection[str] = ("worker",),
    role_backend_pairs: Collection[tuple[str, str]] = (),
) -> SupervisorPlan:
    """Validate and normalize one untrusted supervisor result.

    The returned digest is over the normalized JSON representation and is
    stable across retries.  No database, filesystem, or backend operation is
    performed here.
    """
    if not isinstance(payload, Mapping):
        raise PlanValidationError("plan must be a JSON object")
    unknown = set(payload) - _PLAN_FIELDS
    if unknown:
        raise PlanValidationError(f"unknown plan fields: {sorted(unknown)}")
    missing = _PLAN_FIELDS - set(payload)
    if missing:
        raise PlanValidationError(f"missing plan fields: {sorted(missing)}")

    version = payload["plan_version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise PlanValidationError("plan_version must be an integer")
    if version != PLAN_VERSION:
        raise PlanValidationError(f"unsupported plan_version: {version}")
    summary = _text(payload["summary"], "summary")
    _reject_sensitive(summary, "summary")

    if max_tasks < 1:
        raise ValueError("max_tasks must be at least 1")
    raw_tasks = payload["tasks"]
    if not isinstance(raw_tasks, Sequence) or isinstance(raw_tasks, (str, bytes)):
        raise PlanValidationError("tasks must be a JSON array")
    if not raw_tasks:
        raise PlanValidationError("tasks must not be empty")
    if len(raw_tasks) > max_tasks:
        raise PlanValidationError(
            f"tasks exceed maximum allowed count: {len(raw_tasks)} > {max_tasks}"
        )

    roles = {str(item) for item in allowed_role_ids}
    child_roles = {str(item) for item in allowed_child_role_ids}
    backends = {str(item) for item in allowed_backends}
    pairs = {(str(role), str(backend)) for role, backend in role_backend_pairs}
    existing = {str(item) for item in existing_task_ids}
    tasks: list[SupervisorTaskSpec] = []
    task_ids: set[str] = set()

    for index, raw_task in enumerate(raw_tasks):
        if not isinstance(raw_task, Mapping):
            raise PlanValidationError(f"tasks[{index}] must be a JSON object")
        unknown = set(raw_task) - _TASK_FIELDS
        if unknown:
            raise PlanValidationError(
                f"unknown task fields at index {index}: {sorted(unknown)}"
            )
        missing = _TASK_FIELDS - set(raw_task)
        if missing:
            raise PlanValidationError(
                f"missing task fields at index {index}: {sorted(missing)}"
            )

        task_id = _text(raw_task["task_id"], f"tasks[{index}].task_id")
        role_id = _text(raw_task["role_id"], f"tasks[{index}].role_id")
        backend = _text(raw_task["backend"], f"tasks[{index}].backend")
        prompt = _text(raw_task["prompt"], f"tasks[{index}].prompt")
        access_mode = _text(
            raw_task["access_mode"], f"tasks[{index}].access_mode"
        )
        _reject_sensitive(prompt, f"tasks[{index}].prompt")
        if task_id in task_ids:
            raise PlanValidationError(f"duplicate task_id in plan: {task_id}")
        if task_id in existing:
            raise PlanValidationError(f"task_id already exists: {task_id}")
        if role_id not in roles:
            raise PlanValidationError(f"unknown role_id: {role_id}")
        if role_id not in child_roles:
            raise PlanValidationError(
                f"role_id {role_id} is not allowed for materialized worker tasks"
            )
        if backend not in backends:
            raise PlanValidationError(f"unknown backend: {backend}")
        if pairs and (role_id, backend) not in pairs:
            raise PlanValidationError(
                f"backend {backend} is not available for role {role_id}"
            )
        if access_mode not in {"read_only", "write"}:
            raise PlanValidationError(
                f"unsupported access_mode at index {index}: {access_mode}"
            )

        raw_scope = raw_task["write_scope"]
        if not isinstance(raw_scope, (list, tuple)):
            raise PlanValidationError(
                f"tasks[{index}].write_scope must be a JSON array"
            )
        if access_mode == "read_only" and raw_scope:
            raise PlanValidationError(
                f"read_only task {task_id} cannot declare write_scope"
            )
        if access_mode == "write":
            try:
                write_scope = validate_write_scope_static(raw_scope)
            except ValueError as exc:
                raise PlanValidationError(
                    f"invalid write_scope for {task_id}: {exc}"
                ) from exc
        else:
            write_scope = ()

        raw_dependencies = raw_task["depends_on"]
        if not isinstance(raw_dependencies, (list, tuple)):
            raise PlanValidationError(
                f"tasks[{index}].depends_on must be a JSON array"
            )
        dependencies = tuple(
            _text(item, f"tasks[{index}].depends_on") for item in raw_dependencies
        )
        if len(set(dependencies)) != len(dependencies):
            raise PlanValidationError(f"duplicate dependency in task {task_id}")
        tasks.append(
            SupervisorTaskSpec(
                task_id=task_id,
                role_id=role_id,
                backend=backend,
                prompt=prompt,
                access_mode=access_mode,
                write_scope=write_scope,
                depends_on=dependencies,
            )
        )
        task_ids.add(task_id)

    for task in tasks:
        for dependency in task.depends_on:
            if dependency not in task_ids:
                raise PlanValidationError(
                    f"task {task.task_id} depends on unknown task {dependency}"
                )

    _ensure_acyclic(tasks)
    _ensure_scopes_do_not_conflict(tasks)
    normalized = SupervisorPlan(
        plan_version=version,
        summary=summary,
        tasks=tuple(tasks),
        digest="",
    )
    canonical = json.dumps(
        normalized.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return SupervisorPlan(
        plan_version=normalized.plan_version,
        summary=normalized.summary,
        tasks=normalized.tasks,
        digest=hashlib.sha256(canonical).hexdigest(),
    )


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanValidationError(f"{field} must be a non-empty string")
    if any(ord(char) < 32 and char not in "\t\n\r" for char in value):
        raise PlanValidationError(f"{field} contains control characters")
    return value.strip()


def _reject_sensitive(value: str, field: str) -> None:
    if redact_sensitive(value) != value:
        raise PlanValidationError(f"{field} contains sensitive data")


def _ensure_acyclic(tasks: Sequence[SupervisorTaskSpec]) -> None:
    dependencies = {task.task_id: set(task.depends_on) for task in tasks}
    ready = [task_id for task_id, deps in dependencies.items() if not deps]
    visited = 0
    while ready:
        task_id = ready.pop()
        visited += 1
        for dependent, deps in dependencies.items():
            if task_id in deps:
                deps.remove(task_id)
                if not deps:
                    ready.append(dependent)
    if visited != len(tasks):
        raise PlanValidationError("task dependency graph contains a cycle")


def _ensure_scopes_do_not_conflict(tasks: Sequence[SupervisorTaskSpec]) -> None:
    write_tasks = [task for task in tasks if task.access_mode == "write"]
    for index, left in enumerate(write_tasks):
        for right in write_tasks[index + 1 :]:
            if any(_scope_overlap(a, b) for a in left.write_scope for b in right.write_scope):
                raise PlanValidationError(
                    f"write_scope conflict between {left.task_id} and {right.task_id}"
                )


def _scope_overlap(left: str, right: str) -> bool:
    left_parts = tuple(part.casefold() for part in left.strip("/").split("/") if part)
    right_parts = tuple(part.casefold() for part in right.strip("/").split("/") if part)
    shorter, longer = (
        (left_parts, right_parts)
        if len(left_parts) <= len(right_parts)
        else (right_parts, left_parts)
    )
    return longer[: len(shorter)] == shorter
