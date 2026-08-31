from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class TaskState(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    ACTIVE = "ACTIVE"
    REVIEW = "REVIEW"
    INTEGRATION = "INTEGRATION"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"


class AttemptState(str, Enum):
    ASSIGNED = "ASSIGNED"
    RUNNING = "RUNNING"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    STALE = "STALE"
    FAILED = "FAILED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"


class AccessMode(str, Enum):
    READ_ONLY = "read_only"
    WRITE = "write"


class AgentState(str, Enum):
    STARTING = "STARTING"
    IDLE = "IDLE"
    BUSY = "BUSY"
    DRAINING = "DRAINING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class RoleBindingState(str, Enum):
    ACTIVE = "ACTIVE"
    ENDED = "ENDED"


class SessionState(str, Enum):
    OPENING = "OPENING"
    IDLE = "IDLE"
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class ControllerToken:
    run_id: str
    owner_id: str
    epoch: int
    expires_at: str

    def __post_init__(self) -> None:
        _required(self.run_id, "controller.run_id")
        _required(self.owner_id, "controller.owner_id")
        _required(self.expires_at, "controller.expires_at")
        if self.epoch < 1:
            raise ValueError("controller epoch must be at least 1")


@dataclass(frozen=True)
class AuthorityToken:
    """Business-level supervisor authority lease (distinct from ControllerToken).

    ``ControllerToken`` protects the orchestrator's run control loop; this token
    protects the business Supervisor role (who may dispatch, review, integrate),
    which can be handed off between Codex and CodeBuddy at checkpoints.
    """

    run_id: str
    owner_agent_id: str
    role_id: str
    epoch: int
    expires_at: str

    def __post_init__(self) -> None:
        _required(self.run_id, "authority.run_id")
        _required(self.owner_agent_id, "authority.owner_agent_id")
        _required(self.role_id, "authority.role_id")
        _required(self.expires_at, "authority.expires_at")
        if self.epoch < 1:
            raise ValueError("authority epoch must be at least 1")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _required(value: str, field_name: str) -> str:
    if not value or not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    return value


@dataclass(frozen=True)
class Recipient:
    type: str
    id: str

    def __post_init__(self) -> None:
        _required(self.type, "recipient.type")
        _required(self.id, "recipient.id")


@dataclass(frozen=True)
class MessageEnvelope:
    message_id: str
    team_id: str
    run_id: str
    task_id: str
    sender_agent_id: str
    recipients: tuple[Recipient, ...]
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    correlation_id: str = ""
    sequence: int = 1
    idempotency_key: str = ""
    created_at: str = field(default_factory=utc_now)
    reply_to: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "message_id",
            "team_id",
            "run_id",
            "task_id",
            "sender_agent_id",
            "kind",
            "correlation_id",
            "idempotency_key",
            "created_at",
        ):
            _required(getattr(self, name), name)
        if not self.recipients:
            raise ValueError("recipients must not be empty")
        if self.sequence < 1:
            raise ValueError("sequence must be at least 1")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
