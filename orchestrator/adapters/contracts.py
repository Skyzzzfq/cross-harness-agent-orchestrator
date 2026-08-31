from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Protocol


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    return value


class ProbeStatus(str, Enum):
    READY = "ready"
    BLOCKED = "blocked"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


@dataclass(frozen=True)
class ProbeResult:
    backend: str
    status: ProbeStatus
    version: str | None = None
    entrypoint: str | None = None
    auth: str = "unknown"
    checks: dict[str, bool | str | None] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["status"] = self.status.value
        result["notes"] = list(self.notes)
        return result


class CallState(str, Enum):
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    ORPHANED = "orphaned"

    @property
    def is_terminal(self) -> bool:
        return self not in {self.RUNNING, self.CANCEL_REQUESTED}


@dataclass(frozen=True)
class SessionRef:
    session_id: str
    backend: str
    provider_session_id: str | None = None

    def __post_init__(self) -> None:
        if not self.session_id.strip() or not self.backend.strip():
            raise ValueError("session_id and backend must not be empty")


@dataclass(frozen=True)
class AccessPolicy:
    access_mode: str
    cwd: str
    timeout_seconds: float
    write_scope: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.access_mode not in {"read_only", "write"}:
            raise ValueError("access_mode must be read_only or write")
        if not self.cwd.strip():
            raise ValueError("cwd must not be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.access_mode == "read_only" and self.write_scope:
            raise ValueError("read_only policy cannot declare write_scope")


@dataclass(frozen=True)
class AdapterCallRequest:
    call_id: str
    run_id: str
    task_id: str
    attempt_id: str
    generation: int
    agent_id: str
    session: SessionRef
    prompt: str
    policy: AccessPolicy

    def __post_init__(self) -> None:
        for name in (
            "call_id",
            "run_id",
            "task_id",
            "attempt_id",
            "agent_id",
            "prompt",
        ):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} must not be empty")
        if self.generation < 1:
            raise ValueError("generation must be at least 1")
        if self.session.backend.strip() == "":
            raise ValueError("session backend must not be empty")


@dataclass(frozen=True)
class CallRef:
    call_id: str
    backend: str
    session: SessionRef
    provider_call_id: str | None = None


@dataclass(frozen=True)
class Failure:
    kind: str
    message: str
    retryable: bool = False
    code: str | None = None


@dataclass(frozen=True)
class UsageReport:
    input_tokens: int | None = None
    output_tokens: int | None = None
    turns: int | None = None
    duration_ms: int | None = None
    cost_decimal: str | None = None
    currency: str | None = None
    authoritative_fields: frozenset[str] = frozenset()
    provider_raw: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider_raw",
            _freeze(self.provider_raw),
        )


@dataclass(frozen=True)
class CallSnapshot:
    ref: CallRef
    state: CallState
    started_at: str
    finished_at: str | None = None
    text: str = ""
    structured: Mapping[str, object] = field(default_factory=dict)
    failure: Failure | None = None
    usage: UsageReport | None = None
    backend_invoked: bool = True
    backend_may_still_run: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "structured",
            _freeze(self.structured),
        )


class RunningCall(Protocol):
    @property
    def ref(self) -> CallRef: ...

    async def wait(self, timeout_seconds: float | None = None) -> CallSnapshot: ...

    async def cancel(self, reason: str) -> CallSnapshot: ...


class BackendAdapter(Protocol):
    backend: str

    async def start(self, request: AdapterCallRequest) -> RunningCall: ...
