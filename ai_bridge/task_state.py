from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class TaskState(StrEnum):
    PENDING = "Pending"
    RUNNING = "Running"
    COMPLETED = "Completed"
    FAILED = "Failed"
    CANCELLED = "Cancelled"
    TIMED_OUT = "TimedOut"
    RECOVERING = "Recovering"
    RETRYING = "Retrying"


TERMINAL_STATES = {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED, TaskState.TIMED_OUT}

ALLOWED_TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.PENDING: {TaskState.RUNNING, TaskState.CANCELLED, TaskState.RECOVERING},
    TaskState.RECOVERING: {TaskState.PENDING, TaskState.RUNNING, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.RETRYING: {TaskState.RUNNING, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.RUNNING: {
        TaskState.COMPLETED,
        TaskState.FAILED,
        TaskState.CANCELLED,
        TaskState.TIMED_OUT,
        TaskState.RETRYING,
        TaskState.RECOVERING,
    },
    TaskState.COMPLETED: set(),
    TaskState.FAILED: set(),
    TaskState.CANCELLED: set(),
    TaskState.TIMED_OUT: set(),
}


class InvalidTransition(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskRecord(BaseModel):
    task_id: str = Field(default_factory=lambda: uuid4().hex)
    worker_id: str
    prompt: str
    idempotency_key: str | None = None
    state: TaskState = TaskState.PENDING
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    started_at: str | None = None
    completed_at: str | None = None
    timeout_seconds: float

    def transition(self, target: TaskState, *, error: str | None = None, result: dict[str, Any] | None = None) -> "TaskRecord":
        if target not in ALLOWED_TRANSITIONS[self.state]:
            raise InvalidTransition(f"cannot transition {self.state} -> {target}")
        clone = self.model_copy(deep=True)
        clone.state = target
        clone.updated_at = utc_now()
        if target == TaskState.RUNNING and clone.started_at is None:
            clone.started_at = clone.updated_at
        if target in TERMINAL_STATES:
            clone.completed_at = clone.updated_at
        if error is not None:
            clone.error = error
        if result is not None:
            clone.result = result
        return clone
