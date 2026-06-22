from __future__ import annotations

import asyncio

import pytest

from ai_bridge.config import RecoveryConfig, WorkerConfig
from ai_bridge.manager import TaskManager
from ai_bridge.store import TaskStore
from ai_bridge.task_state import TaskRecord, TaskState


class Workers:
    def __init__(self):
        self.worker = WorkerConfig.model_validate(
            {
                "worker_id": "bob",
                "display_name": "Bob",
                "endpoint_url": "http://worker.local/v1/chat/completions",
                "auth_type": "none",
                "model_name": "test-model",
                "filesystem": {"read": ["/shared"], "write": ["/shared"]},
            }
        )
        self.calls = 0

    def get(self, worker_id: str) -> WorkerConfig:
        return self.worker

    def active_count(self, worker_id: str | None = None) -> int:
        return 0

    async def call(self, *args, **kwargs):
        self.calls += 1
        return {"content": "ok", "raw": {}}


@pytest.mark.asyncio
async def test_running_task_without_idempotency_is_not_replayed_on_restart(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.sqlite3"))
    task = TaskRecord(worker_id="bob", prompt="---\nworking_directory: /shared\n---\nside effect", state=TaskState.RUNNING, timeout_seconds=1)
    store.upsert(task)
    workers = Workers()
    manager = TaskManager(store, workers, recovery=RecoveryConfig(policy="idempotent"))  # type: ignore[arg-type]

    await manager.start()
    await asyncio.sleep(0.02)
    await manager.stop()

    recovered = store.get(task.task_id)
    assert recovered is not None
    assert recovered.state == TaskState.RECOVERING
    assert workers.calls == 0
