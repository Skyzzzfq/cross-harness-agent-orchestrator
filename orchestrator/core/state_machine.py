from __future__ import annotations

from collections.abc import Mapping
from enum import Enum

from orchestrator.core.models import AgentState, AttemptState, SessionState, TaskState


class InvalidTransition(ValueError):
    """Raised when a domain entity attempts an undeclared state transition."""


TASK_TRANSITIONS: Mapping[TaskState, frozenset[TaskState]] = {
    TaskState.PENDING: frozenset({TaskState.READY, TaskState.CANCEL_REQUESTED}),
    TaskState.READY: frozenset({TaskState.ACTIVE, TaskState.CANCEL_REQUESTED}),
    TaskState.ACTIVE: frozenset(
        {
            TaskState.READY,
            TaskState.REVIEW,
            TaskState.FAILED,
            TaskState.CANCEL_REQUESTED,
        }
    ),
    TaskState.REVIEW: frozenset(
        {
            TaskState.READY,
            TaskState.INTEGRATION,
            TaskState.COMPLETED,
            TaskState.CANCEL_REQUESTED,
        }
    ),
    TaskState.INTEGRATION: frozenset({TaskState.COMPLETED, TaskState.REVIEW}),
    TaskState.CANCEL_REQUESTED: frozenset({TaskState.CANCELLED}),
    TaskState.COMPLETED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
}


ATTEMPT_TRANSITIONS: Mapping[AttemptState, frozenset[AttemptState]] = {
    AttemptState.ASSIGNED: frozenset(
        {
            AttemptState.RUNNING,
            AttemptState.STALE,
            AttemptState.FAILED,
            AttemptState.CANCEL_REQUESTED,
        }
    ),
    AttemptState.RUNNING: frozenset(
        {
            AttemptState.SUBMITTED,
            AttemptState.STALE,
            AttemptState.FAILED,
            AttemptState.CANCEL_REQUESTED,
        }
    ),
    AttemptState.SUBMITTED: frozenset(
        {AttemptState.ACCEPTED, AttemptState.REJECTED, AttemptState.STALE}
    ),
    AttemptState.CANCEL_REQUESTED: frozenset({AttemptState.CANCELLED}),
    AttemptState.ACCEPTED: frozenset(),
    AttemptState.REJECTED: frozenset(),
    AttemptState.STALE: frozenset(),
    AttemptState.FAILED: frozenset(),
    AttemptState.CANCELLED: frozenset(),
}


AGENT_TRANSITIONS: Mapping[AgentState, frozenset[AgentState]] = {
    AgentState.STARTING: frozenset(
        {AgentState.IDLE, AgentState.FAILED, AgentState.STOPPED}
    ),
    AgentState.IDLE: frozenset(
        {
            AgentState.BUSY,
            AgentState.DRAINING,
            AgentState.STOPPED,
            AgentState.FAILED,
        }
    ),
    AgentState.BUSY: frozenset(
        {AgentState.IDLE, AgentState.DRAINING, AgentState.FAILED}
    ),
    AgentState.DRAINING: frozenset({AgentState.STOPPED, AgentState.FAILED}),
    AgentState.STOPPED: frozenset(),
    AgentState.FAILED: frozenset(),
}


SESSION_TRANSITIONS: Mapping[SessionState, frozenset[SessionState]] = {
    SessionState.OPENING: frozenset(
        {SessionState.IDLE, SessionState.FAILED, SessionState.CLOSED}
    ),
    SessionState.IDLE: frozenset(
        {SessionState.ACTIVE, SessionState.CLOSED, SessionState.FAILED}
    ),
    SessionState.ACTIVE: frozenset(
        {SessionState.IDLE, SessionState.CLOSED, SessionState.FAILED}
    ),
    SessionState.CLOSED: frozenset(),
    SessionState.FAILED: frozenset({SessionState.CLOSED}),
}


def ensure_transition(
    current: Enum,
    target: Enum,
    transitions: Mapping[Enum, frozenset[Enum]],
) -> None:
    if target not in transitions.get(current, frozenset()):
        raise InvalidTransition(f"invalid transition: {current.value} -> {target.value}")


def ensure_task_transition(current: TaskState, target: TaskState) -> None:
    ensure_transition(current, target, TASK_TRANSITIONS)


def ensure_attempt_transition(current: AttemptState, target: AttemptState) -> None:
    ensure_transition(current, target, ATTEMPT_TRANSITIONS)


def ensure_agent_transition(current: AgentState, target: AgentState) -> None:
    ensure_transition(current, target, AGENT_TRANSITIONS)


def ensure_session_transition(current: SessionState, target: SessionState) -> None:
    ensure_transition(current, target, SESSION_TRANSITIONS)
