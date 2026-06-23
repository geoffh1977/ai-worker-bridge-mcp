from __future__ import annotations

import asyncio
import base64
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from .config import CircuitBreakerConfig, WorkerConfig
from .metrics import MetricsRegistry


class WorkerUnavailable(RuntimeError):
    pass


@dataclass
class CircuitState:
    failures: int = 0
    down_until: float = 0.0


@dataclass
class HealthState:
    healthy: bool | None = None
    checked_at: float = 0.0
    error: str | None = None


class WorkerRegistry:
    def __init__(
        self,
        workers: list[WorkerConfig],
        breaker: CircuitBreakerConfig,
        health_cache_seconds: float = 5.0,
        health_timeout_seconds: float = 0.5,
        metrics: MetricsRegistry | None = None,
    ):
        self._workers = {w.worker_id: w for w in workers}
        self._breaker = breaker
        self._metrics = metrics
        self._circuits = {w.worker_id: CircuitState() for w in workers}
        self._semaphores = {w.worker_id: asyncio.Semaphore(w.max_concurrent_tasks) for w in workers}
        self._active = {w.worker_id: 0 for w in workers}
        self._health = {w.worker_id: HealthState() for w in workers}
        self._health_cache_seconds = health_cache_seconds
        self._health_timeout_seconds = health_timeout_seconds
        self._health_lock = asyncio.Lock()

    def get(self, worker_id: str) -> WorkerConfig:
        try:
            return self._workers[worker_id]
        except KeyError as exc:
            raise KeyError(f"unknown worker_id: {worker_id}") from exc

    def active_count(self, worker_id: str | None = None) -> int:
        if worker_id is not None:
            return self._active.get(worker_id, 0)
        return sum(self._active.values())

    async def list_public(self) -> list[dict[str, Any]]:
        health_by_worker = await self._health_snapshot()
        payload = []
        for worker in self._workers.values():
            health = health_by_worker[worker.worker_id]
            payload.append(
                {
                    "worker_id": worker.worker_id,
                    "display_name": worker.display_name,
                    "model_name": worker.model_name,
                    "capabilities": worker.capabilities,
                    "description": worker.description,
                    "allowed_modes": worker.allowed_modes,
                    "timeout_limits": worker.timeout_limits.model_dump(),
                    "filesystem": {
                        "read": worker.filesystem.read,
                        "write": worker.filesystem.write,
                    },
                    "declared_filesystem": worker.filesystem.model_dump(),
                    "max_concurrent_tasks": worker.max_concurrent_tasks,
                    "active_tasks": self.active_count(worker.worker_id),
                    "status": "up" if health.healthy else "down",
                    "health_checked_at": health.checked_at,
                    "health_error": health.error,
                }
            )
        return payload

    async def _health_snapshot(self) -> dict[str, HealthState]:
        now = time.time()
        async with self._health_lock:
            stale_workers = [
                worker
                for worker in self._workers.values()
                if self._health[worker.worker_id].healthy is None
                or now - self._health[worker.worker_id].checked_at > self._health_cache_seconds
            ]
            if stale_workers:
                results = await asyncio.gather(*(self._probe_worker(worker) for worker in stale_workers), return_exceptions=True)
                checked_at = time.time()
                for worker, result in zip(stale_workers, results, strict=True):
                    if isinstance(result, Exception):
                        self._health[worker.worker_id] = HealthState(False, checked_at, result.__class__.__name__)
                    else:
                        self._health[worker.worker_id] = HealthState(bool(result), checked_at, None if result else "probe failed")
            return dict(self._health)

    async def _probe_worker(self, worker: WorkerConfig) -> bool:
        circuit = self._circuits[worker.worker_id]
        if circuit.down_until > time.monotonic():
            return False
        try:
            async with httpx.AsyncClient(timeout=self._health_timeout_seconds) as client:
                for probe_url in self._probe_urls(worker):
                    response = await client.get(probe_url, headers=self._headers(worker))
                    if response.status_code in {404, 405}:
                        continue
                    return response.status_code < 500
            return False
        except httpx.HTTPError:
            return False

    @staticmethod
    def _probe_urls(worker: WorkerConfig) -> list[str]:
        endpoint = urlsplit(str(worker.endpoint_url))
        origin = urlunsplit((endpoint.scheme, endpoint.netloc, "", "", ""))
        urls: list[str] = []
        path = endpoint.path.rstrip("/")
        if path.endswith("/chat/completions"):
            openai_base = path[: -len("/chat/completions")]
            if openai_base:
                urls.append(urlunsplit((endpoint.scheme, endpoint.netloc, f"{openai_base}/models", "", "")))
        urls.extend([f"{origin}/health", f"{origin}/v0/health"])
        return list(dict.fromkeys(urls))

    def _headers(self, worker: WorkerConfig) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if worker.auth_type == "bearer":
            token = os.getenv(worker.secret_env or "")
            if token:
                headers["Authorization"] = f"Bearer {token}"
        elif worker.auth_type == "basic":
            user = os.getenv(worker.username_env or "")
            password = os.getenv(worker.password_env or "")
            if user or password:
                raw = base64.b64encode(f"{user}:{password}".encode()).decode()
                headers["Authorization"] = f"Basic {raw}"
        return headers

    def assert_available(self, worker_id: str) -> None:
        circuit = self._circuits[worker_id]
        if circuit.down_until > time.monotonic():
            raise WorkerUnavailable(f"worker {worker_id} circuit breaker is open")

    def record_success(self, worker_id: str) -> None:
        self._circuits[worker_id] = CircuitState()

    def record_failure(self, worker_id: str) -> None:
        circuit = self._circuits[worker_id]
        circuit.failures += 1
        if self._metrics:
            self._metrics.inc_worker_failure(worker_id)
        if circuit.failures >= self._breaker.failure_threshold:
            was_closed = circuit.down_until <= time.monotonic()
            circuit.down_until = time.monotonic() + self._breaker.recovery_seconds
            if was_closed and self._metrics:
                self._metrics.inc_circuit_open(worker_id)

    async def call(
        self,
        worker_id: str,
        prompt: str,
        timeout_seconds: float,
        *,
        working_directory: str | None = None,
        task_id: str | None = None,
        idempotency_key: str | None = None,
        dispatch_attempt_id: str | None = None,
        attempt_number: int | None = None,
        recovery_attempt: bool | None = None,
    ) -> dict[str, Any]:
        worker = self.get(worker_id)
        self.assert_available(worker_id)
        messages: list[dict[str, str]] = []
        if worker.default_system_prompt:
            messages.append({"role": "system", "content": worker.default_system_prompt})
        messages.append({"role": "user", "content": prompt})
        payload: dict[str, Any] = {"model": worker.model_name, "messages": messages}
        if working_directory is not None:
            payload["working_directory"] = working_directory
        metadata = {
            k: v
            for k, v in {
                "bridge_task_id": task_id,
                "bridge_idempotency_key": idempotency_key,
                "bridge_dispatch_attempt_id": dispatch_attempt_id,
                "bridge_attempt_number": attempt_number,
                "bridge_recovery_attempt": recovery_attempt,
            }.items()
            if v is not None
        }
        if metadata:
            payload["metadata"] = metadata
        headers = self._headers(worker)
        if task_id:
            headers["X-Bridge-Task-Id"] = task_id
        if idempotency_key:
            headers["X-Bridge-Idempotency-Key"] = idempotency_key
            headers["Idempotency-Key"] = idempotency_key
        if dispatch_attempt_id:
            headers["X-Bridge-Dispatch-Attempt-Id"] = dispatch_attempt_id
            headers["X-Dispatch-Attempt-ID"] = dispatch_attempt_id
        if attempt_number is not None:
            headers["X-Bridge-Attempt-Number"] = str(attempt_number)
        if recovery_attempt is not None:
            headers["X-Bridge-Recovery-Attempt"] = "true" if recovery_attempt else "false"
        started = time.monotonic()
        async with self._semaphores[worker_id]:
            self._active[worker_id] += 1
            try:
                async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                    response = await client.post(str(worker.endpoint_url), headers=headers, json=payload)
                    response.raise_for_status()
                    data = response.json()
                self.record_success(worker_id)
                return self._normalize_response(data)
            except Exception:
                self.record_failure(worker_id)
                raise
            finally:
                self._active[worker_id] -= 1
                if self._metrics:
                    self._metrics.observe_worker_call(worker_id, time.monotonic() - started)

    @staticmethod
    def _normalize_response(data: dict[str, Any]) -> dict[str, Any]:
        content = None
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            content = data.get("content") or data.get("text")
        return {"content": content, "raw": data}
