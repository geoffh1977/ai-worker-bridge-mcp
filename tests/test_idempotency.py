from __future__ import annotations

import asyncio

import pytest

from ai_bridge.config import WorkerConfig
from ai_bridge.manager import TaskManager
from ai_bridge.store import TaskStore


class SlowWorkers:
    def __init__(self):
        self.worker = WorkerConfig.model_validate(
            {
                "worker_id": "bob",
                "display_name": "Bob",
                "endpoint_url": "http://worker.local/v1/chat/completions",
                "auth_type": "none",
                "model_name": "test-model",
                "allowed_modes": ["async"],
                "timeout_limits": {"sync_seconds": 1, "async_seconds": 1},
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
        await asyncio.sleep(0.01)
        return {"content": "ok", "raw": {}}


@pytest.mark.asyncio
async def test_concurrent_idempotency_creates_one_durable_task(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.sqlite3"))
    workers = SlowWorkers()
    manager = TaskManager(store, workers)  # type: ignore[arg-type]
    await manager.start()

    async def submit():
        return await manager.call(worker_id="bob", prompt="---\nworking_directory: /shared\n---\nwork", mode="async", idempotency_key="same-key")

    results = await asyncio.gather(*(submit() for _ in range(10)))
    await asyncio.sleep(0.05)
    await manager.stop()

    assert len({result["task_id"] for result in results}) == 1
    assert store.get_by_idempotency_key("same-key") is not None
    assert workers.calls == 1
