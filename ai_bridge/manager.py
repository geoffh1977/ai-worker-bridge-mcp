from __future__ import annotations

import asyncio
import logging
from typing import Any

from .config import Mode
from .store import TaskStore
from .task_state import TERMINAL_STATES, InvalidTransition, TaskRecord, TaskState
from .workers import WorkerRegistry

log = logging.getLogger(__name__)


class TaskManager:
    def __init__(self, store: TaskStore, workers: WorkerRegistry):
        self.store = store
        self.workers = workers
        self._background: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        for task in self.store.mark_recovering():
            if task.state in TERMINAL_STATES:
                continue
            await self._schedule(task)

    async def stop(self) -> None:
        for task in list(self._background.values()):
            task.cancel()
        if self._background:
            await asyncio.gather(*self._background.values(), return_exceptions=True)
        self._background.clear()
        self._started = False

    def has_active_tasks(self) -> bool:
        return any(not task.done() for task in self._background.values())

    async def update_workers(self, workers: WorkerRegistry) -> None:
        async with self._lock:
            self.workers = workers

    async def call(
        self,
        *,
        worker_id: str,
        prompt: str,
        mode: Mode = "sync",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        worker = self.workers.get(worker_id)
        if mode not in worker.allowed_modes:
            return {"ok": False, "error": f"mode {mode} is not allowed for worker {worker_id}"}
        if mode == "sync":
            result = await self.workers.call(worker_id, prompt, worker.timeout_limits.sync_seconds)
            return {"ok": True, "mode": "sync", "worker_id": worker_id, "result": result}

        if idempotency_key:
            existing = self.store.get_by_idempotency_key(idempotency_key)
            if existing:
                return self._task_response(existing)
        task = TaskRecord(
            worker_id=worker_id,
            prompt=prompt,
            idempotency_key=idempotency_key,
            timeout_seconds=worker.timeout_limits.async_seconds,
        )
        self.store.upsert(task)
        await self._schedule(task)
        return self._task_response(task)

    async def check(self, task_id: str) -> dict[str, Any]:
        task = self.store.get(task_id)
        if not task:
            return {"ok": False, "error": "task not found", "task_id": task_id}
        return self._task_response(task)

    async def cancel(self, task_id: str) -> dict[str, Any]:
        async with self._lock:
            task = self.store.get(task_id)
            if not task:
                return {"ok": False, "error": "task not found", "task_id": task_id}
            if task.state in TERMINAL_STATES:
                return self._task_response(task)
            bg = self._background.get(task_id)
            if bg:
                bg.cancel()
            try:
                updated = task.transition(TaskState.CANCELLED, error="cancelled by caller")
            except InvalidTransition as exc:
                return {"ok": False, "error": str(exc), "task_id": task_id}
            self.store.upsert(updated)
            return self._task_response(updated)

    async def _schedule(self, task: TaskRecord) -> None:
        async with self._lock:
            if task.task_id in self._background and not self._background[task.task_id].done():
                return
            handle = asyncio.create_task(self._run_task(task.task_id), name=f"worker-task-{task.task_id}")
            self._background[task.task_id] = handle
            handle.add_done_callback(lambda _: self._background.pop(task.task_id, None))

    async def _run_task(self, task_id: str) -> None:
        task = self.store.get(task_id)
        if not task or task.state in TERMINAL_STATES:
            return
        try:
            if task.state == TaskState.RECOVERING:
                task = task.transition(TaskState.PENDING)
            if task.state == TaskState.PENDING:
                task = task.transition(TaskState.RUNNING)
                self.store.upsert(task)
            result = await self.workers.call(task.worker_id, task.prompt, task.timeout_seconds)
            latest = self.store.get(task_id)
            if latest and latest.state not in TERMINAL_STATES:
                self.store.upsert(latest.transition(TaskState.COMPLETED, result=result))
        except asyncio.CancelledError:
            latest = self.store.get(task_id)
            if latest and latest.state not in TERMINAL_STATES:
                try:
                    self.store.upsert(latest.transition(TaskState.CANCELLED, error="background task cancelled"))
                except InvalidTransition:
                    pass
            raise
        except TimeoutError as exc:
            self._fail_terminal(task_id, TaskState.TIMED_OUT, str(exc) or "worker timed out")
        except Exception as exc:  # noqa: BLE001 - bridge must persist all worker failures
            latest = self.store.get(task_id)
            message = str(exc) or exc.__class__.__name__
            if latest and latest.state not in TERMINAL_STATES:
                target = TaskState.TIMED_OUT if "timeout" in message.lower() else TaskState.FAILED
                self._fail_terminal(task_id, target, message)
            log.exception("async worker task failed", extra={"task_id": task_id, "event": "task_failed"})

    def _fail_terminal(self, task_id: str, state: TaskState, error: str) -> None:
        latest = self.store.get(task_id)
        if not latest or latest.state in TERMINAL_STATES:
            return
        try:
            self.store.upsert(latest.transition(state, error=error))
        except InvalidTransition:
            pass

    @staticmethod
    def _task_response(task: TaskRecord) -> dict[str, Any]:
        return {
            "ok": True,
            "taskId": task.task_id,
            "task_id": task.task_id,
            "worker_id": task.worker_id,
            "state": task.state.value,
            "result": task.result,
            "error": task.error,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            "completed_at": task.completed_at,
        }
