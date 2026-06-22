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
    ):
        self._workers = {w.worker_id: w for w in workers}
        self._breaker = breaker
        self._circuits = {w.worker_id: CircuitState() for w in workers}
        self._semaphores = {w.worker_id: asyncio.Semaphore(w.max_concurrent_tasks) for w in workers}
        self._health = {w.worker_id: HealthState() for w in workers}
        self._health_cache_seconds = health_cache_seconds
        self._health_timeout_seconds = health_timeout_seconds
        self._health_lock = asyncio.Lock()

    def get(self, worker_id: str) -> WorkerConfig:
        try:
            return self._workers[worker_id]
        except KeyError as exc:
            raise KeyError(f"unknown worker_id: {worker_id}") from exc

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
                    "filesystem": worker.filesystem.model_dump(),
                    "max_concurrent_tasks": worker.max_concurrent_tasks,
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
                results = await asyncio.gather(
                    *(self._probe_worker(worker) for worker in stale_workers), return_exceptions=True
                )
                checked_at = time.time()
                for worker, result in zip(stale_workers, results, strict=True):
                    if isinstance(result, Exception):
                        self._health[worker.worker_id] = HealthState(
                            healthy=False,
                            checked_at=checked_at,
                            error=result.__class__.__name__,
                        )
                    else:
                        self._health[worker.worker_id] = HealthState(
                            healthy=bool(result),
                            checked_at=checked_at,
                            error=None if result else "probe failed",
                        )
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
        if circuit.failures >= self._breaker.failure_threshold:
            circuit.down_until = time.monotonic() + self._breaker.recovery_seconds

    async def call(
        self,
        worker_id: str,
        prompt: str,
        timeout_seconds: float,
        *,
        working_directory: str | None = None,
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
        async with self._semaphores[worker_id]:
            try:
                async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                    response = await client.post(
                        str(worker.endpoint_url),
                        headers=self._headers(worker),
                        json=payload,
                    )
                    response.raise_for_status()
                    data = response.json()
                self.record_success(worker_id)
                return self._normalize_response(data)
            except Exception:
                self.record_failure(worker_id)
                raise

    @staticmethod
    def _normalize_response(data: dict[str, Any]) -> dict[str, Any]:
        content = None
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            content = data.get("content") or data.get("text")
        return {"content": content, "raw": data}
