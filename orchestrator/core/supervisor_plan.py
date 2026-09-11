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

PLAN_VERSION = 2
SUPPORTED_PLAN_VERSIONS = frozenset({1, PLAN_VERSION})
_PLAN_FIELDS = frozenset(
    {
        "plan_version",
        "revision",
        "summary",
        "worker_concurrency",
        "task_cap",
        "permissions",
        "budget",
        "tasks",
    }
)
_TASK_FIELDS = frozenset(
    {
        "task_id",
        "role_id",
        "backend",
        "prompt",
        "access_mode",
        "write_scope",
        "depends_on",
        "acceptance_criteria",
        "input_refs",
        "task_kind",
        "output_contract",
        "required_model",
        "required_provider_id",
        "budget",
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
    acceptance_criteria: tuple[str, ...] = ()
    input_refs: tuple[Any, ...] = ()
    task_kind: str = "implementation"
    output_contract: Any = None
    required_model: str | None = None
    required_provider_id: str | None = None
    budget: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "role_id": self.role_id,
            "backend": self.backend,
            "prompt": self.prompt,
            "access_mode": self.access_mode,
            "write_scope": list(self.write_scope),
            "depends_on": list(self.depends_on),
            "acceptance_criteria": list(self.acceptance_criteria),
            "input_refs": list(self.input_refs),
            "task_kind": self.task_kind,
            "output_contract": self.output_contract,
            "required_model": self.required_model,
            "required_provider_id": self.required_provider_id,
            "budget": self.budget,
        }


@dataclass(frozen=True)
class SupervisorPlan:
    plan_version: int
    summary: str
    tasks: tuple[SupervisorTaskSpec, ...]
    digest: str
    revision: int = 1
    worker_concurrency: int = 1
    task_cap: int = 1
    permissions: Any = None
    budget: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_version": self.plan_version,
            "revision": self.revision,
            "summary": self.summary,
            "worker_concurrency": self.worker_concurrency,
            "task_cap": self.task_cap,
            "permissions": self.permissions,
            "budget": self.budget,
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
    role_model_pairs: Collection[tuple[str, str, str]] = (),
    max_worker_concurrency: int | None = None,
) -> SupervisorPlan:
    """Validate and normalize one untrusted supervisor result.

    The returned digest is over the normalized JSON representation and is
    stable across retries.  No database, filesystem, or backend operation is
    performed here.
    """
    if not isinstance(payload, Mapping):
        raise PlanValidationError("plan must be a JSON object")
    # Some model families use a natural-language field name for the plan
    # synopsis.  Treat it as the public alias of ``summary`` before applying
    # the strict contract; the normalized plan still stores only ``summary``.
    normalized_payload = dict(payload)
    if "summary" not in normalized_payload and "supervisor_response" in normalized_payload:
        normalized_payload["summary"] = normalized_payload.pop("supervisor_response")
    raw_tasks = normalized_payload.get("tasks")
    if isinstance(raw_tasks, Sequence) and not isinstance(raw_tasks, (str, bytes)):
        normalized_tasks: list[Any] = []
        for raw_task in raw_tasks:
            if isinstance(raw_task, Mapping):
                normalized_task = dict(raw_task)
                if "prompt" not in normalized_task and "instruction" in normalized_task:
                    normalized_task["prompt"] = normalized_task.pop("instruction")
                normalized_tasks.append(normalized_task)
            else:
                normalized_tasks.append(raw_task)
        normalized_payload["tasks"] = normalized_tasks
    unknown = set(normalized_payload) - _PLAN_FIELDS
    if unknown:
        raise PlanValidationError(f"unknown plan fields: {sorted(unknown)}")
    missing = {"plan_version", "summary", "tasks"} - set(normalized_payload)
    if missing:
        raise PlanValidationError(f"missing plan fields: {sorted(missing)}")

    version = normalized_payload["plan_version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise PlanValidationError("plan_version must be an integer")
    if version not in SUPPORTED_PLAN_VERSIONS:
        raise PlanValidationError(f"unsupported plan_version: {version}")
    if version >= 2:
        required_v2 = {"revision", "worker_concurrency", "task_cap"} - set(normalized_payload)
        if required_v2:
            raise PlanValidationError(f"missing plan fields: {sorted(required_v2)}")
    revision = _positive_int(normalized_payload.get("revision", 1), "revision")
    worker_concurrency = _positive_int(
        normalized_payload.get("worker_concurrency", 1), "worker_concurrency"
    )
    task_cap = _positive_int(normalized_payload.get("task_cap", max_tasks), "task_cap")
    if task_cap > max_tasks:
        raise PlanValidationError(f"task_cap exceeds maximum allowed count: {task_cap} > {max_tasks}")
    if max_worker_concurrency is not None and worker_concurrency > max_worker_concurrency:
        raise PlanValidationError(
            f"worker_concurrency exceeds available slots: {worker_concurrency} > {max_worker_concurrency}"
        )
    if worker_concurrency > task_cap:
        raise PlanValidationError("worker_concurrency cannot exceed task_cap")
    summary = _text(normalized_payload["summary"], "summary")
    _reject_sensitive(summary, "summary")

    if max_tasks < 1:
        raise ValueError("max_tasks must be at least 1")
    raw_tasks = normalized_payload["tasks"]
    if not isinstance(raw_tasks, Sequence) or isinstance(raw_tasks, (str, bytes)):
        raise PlanValidationError("tasks must be a JSON array")
    if not raw_tasks:
        raise PlanValidationError("tasks must not be empty")
    if len(raw_tasks) > task_cap:
        raise PlanValidationError(
            f"tasks exceed maximum task_cap: {len(raw_tasks)} > {task_cap}"
        )

    roles = {str(item) for item in allowed_role_ids}
    child_roles = {str(item) for item in allowed_child_role_ids}
    backends = {str(item) for item in allowed_backends}
    pairs = {(str(role), str(backend)) for role, backend in role_backend_pairs}
    model_pairs = {
        (str(role), str(backend), str(model)) for role, backend, model in role_model_pairs
    }
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
        missing = {"task_id", "role_id", "backend", "prompt", "access_mode", "write_scope", "depends_on"} - set(raw_task)
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
        required_model = _optional_text(raw_task.get("required_model"), "required_model")
        required_provider_id = _optional_text(
            raw_task.get("required_provider_id"), "required_provider_id"
        )
        if required_model and model_pairs and (role_id, backend, required_model) not in model_pairs:
            raise PlanValidationError(
                f"model {required_model} is not available for role {role_id} and backend {backend}"
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

        if version >= 2:
            required_task_fields = {
                "acceptance_criteria",
                "input_refs",
                "task_kind",
                "output_contract",
            } - set(raw_task)
            if required_task_fields:
                raise PlanValidationError(
                    f"missing task fields at index {index}: {sorted(required_task_fields)}"
                )
        raw_acceptance = raw_task.get("acceptance_criteria", ())
        if not isinstance(raw_acceptance, (list, tuple)) or any(
            not isinstance(item, str) or not item.strip() for item in raw_acceptance
        ):
            raise PlanValidationError(
                f"tasks[{index}].acceptance_criteria must be an array of strings"
            )
        acceptance_criteria = tuple(item.strip() for item in raw_acceptance)
        raw_refs = raw_task.get("input_refs", ())
        if not isinstance(raw_refs, (list, tuple)):
            raise PlanValidationError(f"tasks[{index}].input_refs must be an array")
        input_refs = tuple(_json_safe(item, f"tasks[{index}].input_refs") for item in raw_refs)
        task_kind = _text(raw_task.get("task_kind", "implementation"), f"tasks[{index}].task_kind")
        output_contract = _json_safe(raw_task.get("output_contract"), f"tasks[{index}].output_contract")
        task_budget = _json_safe(raw_task.get("budget"), f"tasks[{index}].budget")

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
                acceptance_criteria=acceptance_criteria,
                input_refs=input_refs,
                task_kind=task_kind,
                output_contract=output_contract,
                required_model=required_model,
                required_provider_id=required_provider_id,
                budget=task_budget,
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
        revision=revision,
        worker_concurrency=worker_concurrency,
        task_cap=task_cap,
        permissions=_json_safe(normalized_payload.get("permissions"), "permissions"),
        budget=_json_safe(normalized_payload.get("budget"), "budget"),
    )
    canonical = json.dumps(
        normalized.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return SupervisorPlan(
        plan_version=normalized.plan_version,
        summary=normalized.summary,
        tasks=normalized.tasks,
        digest=hashlib.sha256(canonical).hexdigest(),
        revision=normalized.revision,
        worker_concurrency=normalized.worker_concurrency,
        task_cap=normalized.task_cap,
        permissions=normalized.permissions,
        budget=normalized.budget,
    )


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanValidationError(f"{field} must be a non-empty string")
    if any(ord(char) < 32 and char not in "\t\n\r" for char in value):
        raise PlanValidationError(f"{field} contains control characters")
    return value.strip()


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PlanValidationError(f"{field} must be a positive integer")
    return value


def _optional_text(value: Any, field: str) -> str | None:
    if value is None or value == "":
        return None
    return _text(value, field)


def _json_safe(value: Any, field: str) -> Any:
    """Ensure plan metadata can be serialized without executing arbitrary objects."""
    try:
        json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise PlanValidationError(f"{field} must be JSON-compatible") from exc
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item, field) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, field) for item in value]
    return value


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
