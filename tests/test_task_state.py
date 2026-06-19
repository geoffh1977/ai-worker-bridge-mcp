import pytest

from ai_bridge.task_state import InvalidTransition, TaskRecord, TaskState


def test_task_state_allows_expected_happy_path_transitions():
    task = TaskRecord(worker_id="bob", prompt="hello", timeout_seconds=1)

    running = task.transition(TaskState.RUNNING)
    completed = running.transition(TaskState.COMPLETED, result={"content": "ok"})

    assert completed.state == TaskState.COMPLETED
    assert completed.result == {"content": "ok"}
    assert completed.started_at is not None
    assert completed.completed_at is not None


def test_cancelled_cannot_transition_to_failed():
    task = TaskRecord(worker_id="bob", prompt="hello", timeout_seconds=1)
    cancelled = task.transition(TaskState.CANCELLED, error="caller cancelled")

    with pytest.raises(InvalidTransition):
        cancelled.transition(TaskState.FAILED, error="late worker failure")


def test_running_can_timeout_and_timeout_is_terminal():
    task = TaskRecord(worker_id="bob", prompt="slow", timeout_seconds=1).transition(TaskState.RUNNING)
    timed_out = task.transition(TaskState.TIMED_OUT, error="timeout")

    assert timed_out.state == TaskState.TIMED_OUT
    with pytest.raises(InvalidTransition):
        timed_out.transition(TaskState.RUNNING)


def test_recovering_can_return_to_pending_then_running():
    task = TaskRecord(worker_id="bob", prompt="recover", timeout_seconds=1)
    recovering = task.transition(TaskState.RECOVERING)
    pending = recovering.transition(TaskState.PENDING)
    running = pending.transition(TaskState.RUNNING)

    assert running.state == TaskState.RUNNING
