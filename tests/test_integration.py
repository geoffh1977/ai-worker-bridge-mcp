from __future__ import annotations

import asyncio

import pytest

from ai_bridge.config import CircuitBreakerConfig, TimeoutLimits, WorkerConfig
from ai_bridge.manager import TaskManager
from ai_bridge.store import TaskStore
from ai_bridge.task_state import TaskRecord, TaskState
from ai_bridge.workers import WorkerRegistry, WorkerUnavailable


class FakeWorkers:
    def __init__(self, result: str = "ok", delay: float = 0.0, fail: Exception | None = None):
        self.worker = WorkerConfig(
            worker_id="bob",
            display_name="Bob",
            endpoint_url="http://worker.local/v1/chat/completions",
            auth_type="none",
            model_name="test-model",
            allowed_modes=["sync", "async"],
            timeout_limits=TimeoutLimits(sync_seconds=0.05, async_seconds=1),
            max_concurrent_tasks=2,
        )
        self.result = result
        self.delay = delay
        self.fail = fail
        self.calls = 0

    def get(self, worker_id: str) -> WorkerConfig:
        assert worker_id == "bob"
        return self.worker

    def list_public(self):
        return [{"worker_id": "bob", "status": "up"}]

    async def call(
        self,
        worker_id: str,
        prompt: str,
        timeout_seconds: float,
        *,
        working_directory: str | None = None,
    ):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail
        return {"content": f"{self.result}:{prompt}", "raw": {"ok": True}}


@pytest.mark.asyncio
async def test_async_task_persists_and_recovers_after_manager_restart(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.sqlite3"))
    task = TaskRecord(
        worker_id="bob",
        prompt="---\nworking_directory: /\n---\nsurvive",
        state=TaskState.RUNNING,
        timeout_seconds=1,
    )
    store.upsert(task)

    manager = TaskManager(store, FakeWorkers(result="recovered"))
    await manager.start()

    for _ in range(50):
        checked = await manager.check(task.task_id)
        if checked["state"] == "Completed":
            break
        await asyncio.sleep(0.02)
    await manager.stop()

    checked = await manager.check(task.task_id)
    assert checked["state"] == "Completed"
    assert checked["result"]["content"] == "recovered:---\nworking_directory: /\n---\nsurvive"


@pytest.mark.asyncio
async def test_async_timeout_is_recorded_as_timed_out(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.sqlite3"))
    manager = TaskManager(store, FakeWorkers(fail=TimeoutError("worker timeout")))
    await manager.start()

    created = await manager.call(
        worker_id="bob",
        prompt="---\nworking_directory: /\n---\nslow",
        mode="async",
    )
    for _ in range(50):
        checked = await manager.check(created["taskId"])
        if checked["state"] == "TimedOut":
            break
        await asyncio.sleep(0.02)
    await manager.stop()

    assert checked["state"] == "TimedOut"
    assert "timeout" in checked["error"]


@pytest.mark.asyncio
async def test_sync_worker_circuit_breaker_failover_opens_after_threshold():
    worker = WorkerConfig(
        worker_id="bob",
        display_name="Bob",
        endpoint_url="http://127.0.0.1:9/v1/chat/completions",
        auth_type="none",
        model_name="test-model",
        timeout_limits=TimeoutLimits(sync_seconds=0.001, async_seconds=0.001),
        max_concurrent_tasks=1,
    )
    registry = WorkerRegistry([worker], CircuitBreakerConfig(failure_threshold=1, recovery_seconds=30))

    with pytest.raises(Exception):
        await registry.call("bob", "hello", 0.001)
    with pytest.raises(WorkerUnavailable):
        registry.assert_available("bob")
