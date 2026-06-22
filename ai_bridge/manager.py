from __future__ import annotations

import asyncio
import logging
from typing import Any

from .audit import AuditLogger
from .config import LimitsConfig, Mode, RecoveryConfig
from .exceptions import SaturationError
from .metrics import MetricsRegistry
from .permissions import resolve_working_directory
from .store import TaskStore
from .task_state import ACTIVE_STATES, QUEUED_STATES, TERMINAL_STATES, InvalidTransition, TaskRecord, TaskState
from .workers import WorkerRegistry

log = logging.getLogger(__name__)


class TaskManager:
    def __init__(
        self,
        store: TaskStore,
        workers: WorkerRegistry,
        *,
        recovery: RecoveryConfig | None = None,
        limits: LimitsConfig | None = None,
        audit: AuditLogger | None = None,
        metrics: MetricsRegistry | None = None,
    ):
        self.store = store
        self.workers = workers
        self.recovery = recovery or RecoveryConfig()
        self.limits = limits or LimitsConfig()
        self.audit = audit or AuditLogger(None, enabled=False)
        self.metrics = metrics or MetricsRegistry()
        self._background: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()
        self._sync_limit = asyncio.Semaphore(self.limits.sync_active_tasks)
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        for task in self.store.mark_recovering():
            if task.state in TERMINAL_STATES:
                continue
            if task.state == TaskState.RECOVERING and not self._recovery_allows_replay(task):
                self.audit.emit("task_recovery_deferred", outcome="manual_required", worker_id=task.worker_id, task_id=task.task_id)
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

    def active_count(self) -> int:
        return sum(1 for task in self._background.values() if not task.done())

    def queued_count(self) -> int:
        return self.store.count_by_states(QUEUED_STATES)

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
        actor: str | None = None,
        source_ip: str | None = None,
    ) -> dict[str, Any]:
        worker = self.workers.get(worker_id)
        working_directory = resolve_working_directory(worker, prompt)
        if mode not in worker.allowed_modes:
            return {"ok": False, "error": f"mode {mode} is not allowed for worker {worker_id}"}
        if mode == "sync":
            self._enforce_sync_limits(worker_id)
            call_kwargs: dict[str, Any] = {"working_directory": working_directory}
            if idempotency_key:
                call_kwargs["idempotency_key"] = idempotency_key
            async with self._sync_limit:
                result = await self.workers.call(
                    worker_id,
                    prompt,
                    worker.timeout_limits.sync_seconds,
                    **call_kwargs,
                )
            self.metrics.inc_task_created(worker_id, "sync")
            self.audit.emit("task_submission", outcome="accepted", actor=actor, source_ip=source_ip, worker_id=worker_id, mode="sync")
            return {"ok": True, "mode": "sync", "worker_id": worker_id, "result": result}

        self._enforce_async_limits(worker_id)
        task = TaskRecord(
            worker_id=worker_id,
            prompt=prompt,
            idempotency_key=idempotency_key,
            timeout_seconds=worker.timeout_limits.async_seconds,
            working_directory=working_directory,
        )
        task, created = self.store.create_or_get(task)
        if created:
            self.metrics.inc_task_created(worker_id, "async")
            self.audit.emit("task_submission", outcome="accepted", actor=actor, source_ip=source_ip, worker_id=worker_id, task_id=task.task_id, mode="async")
            await self._schedule(task)
        else:
            self.audit.emit("task_submission", outcome="idempotent_replay", actor=actor, source_ip=source_ip, worker_id=worker_id, task_id=task.task_id, mode="async")
        return self._task_response(task)

    async def check(self, task_id: str) -> dict[str, Any]:
        task = self.store.get(task_id)
        if not task:
            return {"ok": False, "error": "task not found", "task_id": task_id}
        return self._task_response(task)

    async def cancel(self, task_id: str, *, actor: str | None = None, source_ip: str | None = None) -> dict[str, Any]:
        async with self._lock:
            task = self.store.get(task_id)
            if not task:
                self.audit.emit("task_cancel", outcome="not_found", actor=actor, source_ip=source_ip, task_id=task_id)
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
            self.metrics.inc_task_completed(updated.worker_id, updated.state.value)
            self.audit.emit("task_cancel", outcome="cancelled", actor=actor, source_ip=source_ip, worker_id=updated.worker_id, task_id=task_id)
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
            if self.recovery.delay_seconds and task.state == TaskState.RECOVERING:
                await asyncio.sleep(self.recovery.delay_seconds)
            if task.working_directory is None:
                worker = self.workers.get(task.worker_id)
                task.working_directory = resolve_working_directory(worker, task.prompt)
                self.store.upsert(task)
            if task.state == TaskState.RECOVERING:
                task = task.transition(TaskState.PENDING)
            if task.state == TaskState.PENDING:
                task = task.next_attempt().transition(TaskState.RUNNING)
                self.store.upsert(task)
            result = await self.workers.call(
                task.worker_id,
                task.prompt,
                task.timeout_seconds,
                working_directory=task.working_directory,
                idempotency_key=task.idempotency_key,
                dispatch_attempt_id=task.dispatch_attempt_id,
            )
            latest = self.store.get(task_id)
            if latest and latest.state not in TERMINAL_STATES:
                completed = latest.transition(TaskState.COMPLETED, result=result)
                self.store.upsert(completed)
                self.metrics.inc_task_completed(completed.worker_id, completed.state.value)
        except asyncio.CancelledError:
            latest = self.store.get(task_id)
            if latest and latest.state not in TERMINAL_STATES:
                try:
                    cancelled = latest.transition(TaskState.CANCELLED, error="background task cancelled")
                    self.store.upsert(cancelled)
                    self.metrics.inc_task_completed(cancelled.worker_id, cancelled.state.value)
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
            failed = latest.transition(state, error=error)
            self.store.upsert(failed)
            self.metrics.inc_task_completed(failed.worker_id, failed.state.value)
            self.audit.emit("worker_failure", outcome=state.value, worker_id=failed.worker_id, task_id=task_id, error_category=error.split(":", 1)[0][:80])
        except InvalidTransition:
            pass

    def _recovery_allows_replay(self, task: TaskRecord) -> bool:
        if self.recovery.policy == "always":
            return True
        if self.recovery.policy == "idempotent":
            return bool(task.idempotency_key)
        return task.state == TaskState.PENDING

    def _enforce_async_limits(self, worker_id: str) -> None:
        global_queued = self.store.count_by_states(QUEUED_STATES)
        if global_queued >= self.limits.global_pending_tasks:
            raise SaturationError("global pending task queue is full", status_code=503, scope="global_pending")
        worker_queued = self.store.count_by_states(QUEUED_STATES, worker_id=worker_id)
        if worker_queued >= self.limits.per_worker_pending_tasks:
            raise SaturationError("worker pending task queue is full", status_code=503, scope="worker_pending")
        if self.active_count() >= self.limits.global_active_tasks:
            raise SaturationError("global active task limit reached", status_code=503, scope="global_active")
        per_worker_active = self.limits.per_worker_active_tasks
        if per_worker_active is not None and self.store.count_by_states(ACTIVE_STATES, worker_id=worker_id) >= per_worker_active:
            raise SaturationError("worker active task limit reached", status_code=503, scope="worker_active")

    def _enforce_sync_limits(self, worker_id: str) -> None:
        per_worker_active = self.limits.per_worker_active_tasks
        if per_worker_active is not None and self.workers.active_count(worker_id) >= per_worker_active:
            raise SaturationError("worker sync capacity reached", status_code=503, scope="worker_sync")

    @staticmethod
    def _task_response(task: TaskRecord) -> dict[str, Any]:
        return {
            "ok": True,
            "taskId": task.task_id,
            "task_id": task.task_id,
            "worker_id": task.worker_id,
            "working_directory": task.working_directory,
            "idempotency_key": task.idempotency_key,
            "dispatch_attempt_id": task.dispatch_attempt_id,
            "attempt_count": task.attempt_count,
            "state": task.state.value,
            "result": task.result,
            "error": task.error,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            "completed_at": task.completed_at,
        }
